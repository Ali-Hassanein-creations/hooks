import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import any_, func, insert, literal, select, text

from webhooks.auth import DbSession, TenantId
from webhooks.models import Delivery, Endpoint, Event

router = APIRouter(prefix="/v1/events", tags=["events"])

RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "600"))

# One row per tenant holding the current minute and its count; the upsert is atomic, so it
# holds across any number of API replicas without Redis.
# ponytail: fixed window lets a tenant burst up to 2x the limit across a minute boundary;
# switch to a token bucket if that matters
_COUNT_REQUEST = text("""
INSERT INTO rate_limits AS r (tenant_id, window_start, count)
VALUES (:tenant, date_trunc('minute', now()), 1)
ON CONFLICT (tenant_id) DO UPDATE SET
    count = CASE WHEN r.window_start = EXCLUDED.window_start THEN r.count + 1 ELSE 1 END,
    window_start = EXCLUDED.window_start
RETURNING count AS n, 60 - floor(extract(second FROM now()))::int AS retry_after
""")


class EventIn(BaseModel):
    event_type: str = Field(min_length=1, max_length=200)
    payload: dict[str, Any]


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    endpoint_id: uuid.UUID
    status: str
    attempt_count: int
    next_attempt_at: datetime


class EventDetail(EventOut):
    deliveries: list[DeliveryOut]


@router.post("", status_code=202)
async def ingest(
    body: EventIn,
    response: Response,
    tenant: TenantId,
    session: DbSession,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=255)],
) -> EventOut:
    """Write the event and one pending delivery per matching endpoint in ONE transaction.

    This is the transactional outbox: workers poll `deliveries` directly, so there is no
    separate queue push that could be lost (or run ahead) if we crash mid-request.
    """
    # Counted and committed on its own, so it sticks even when the ingest below rolls back.
    limit = (await session.execute(_COUNT_REQUEST, {"tenant": tenant})).one()
    await session.commit()
    if limit.n > RATE_LIMIT_PER_MINUTE:
        raise HTTPException(
            429, "rate limit exceeded", headers={"Retry-After": str(limit.retry_after)}
        )

    # Serialises concurrent requests for the same key: a client retrying while its first
    # request is still in flight waits here, then sees the committed event below.
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"{tenant}:{idempotency_key}", 0)))
    )
    # Canonical JSON so key order in the client's body doesn't change the hash.
    request_hash = hashlib.sha256(
        json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = await session.scalar(
        select(Event).where(Event.tenant_id == tenant, Event.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if existing.request_hash not in (None, request_hash):
            await session.rollback()
            raise HTTPException(422, "Idempotency-Key was already used with a different body")
        out = EventOut.model_validate(existing)
        await session.rollback()  # releases the advisory lock
        response.status_code = 200
        return out

    event = Event(
        tenant_id=tenant,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        **body.model_dump(),
    )
    session.add(event)
    await session.flush()
    await session.execute(
        insert(Delivery).from_select(
            ["tenant_id", "event_id", "endpoint_id"],
            select(Endpoint.tenant_id, literal(event.id), Endpoint.id).where(
                Endpoint.tenant_id == tenant,
                Endpoint.status == "enabled",
                Endpoint.deleted_at.is_(None),
                literal(body.event_type) == any_(Endpoint.event_types),
            ),
        )
    )
    await session.commit()
    await session.refresh(event)
    return EventOut.model_validate(event)


@router.get("/{id}")
async def get_event(id: uuid.UUID, tenant: TenantId, session: DbSession) -> EventDetail:
    event = await session.scalar(select(Event).where(Event.id == id, Event.tenant_id == tenant))
    if event is None:
        raise HTTPException(404, "event not found")
    deliveries = await session.scalars(
        select(Delivery).where(Delivery.event_id == id, Delivery.tenant_id == tenant)
    )
    return EventDetail(
        **EventOut.model_validate(event).model_dump(),
        deliveries=[DeliveryOut.model_validate(d) for d in deliveries],
    )
