import base64
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import func, select, tuple_, update

from webhooks import ssrf
from webhooks.auth import DbSession, TenantId
from webhooks.db import encrypt
from webhooks.models import Delivery, Endpoint

router = APIRouter(prefix="/v1/endpoints", tags=["endpoints"])

EventTypes = list[str]


def check_url(url: str) -> str:
    ssrf.check_static(url)
    return url


async def check_destination(url: str) -> None:
    """Resolve now so a tenant learns at registration that a URL will never be delivered to.
    The worker re-checks before every attempt, because DNS can change in between."""
    try:
        await ssrf.resolve(url)
    except (ssrf.BlockedURL, ssrf.UnresolvableHost) as exc:
        raise HTTPException(422, str(exc)) from exc


def _new_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


Url = Annotated[str, Field(max_length=2048), AfterValidator(check_url)]


class EndpointIn(BaseModel):
    url: Url
    event_types: EventTypes = Field(min_length=1)
    description: str | None = Field(default=None, max_length=500)


class EndpointPatch(BaseModel):
    url: Url | None = None
    event_types: EventTypes | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, max_length=500)
    status: Literal["enabled", "disabled"] | None = None


class EndpointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    url: str
    event_types: EventTypes
    description: str | None
    status: str
    created_at: datetime


class EndpointCreated(EndpointOut):
    secret: str


class RotateIn(BaseModel):
    # How long the old secret keeps signing, so receivers can switch over without a gap.
    overlap_hours: int = Field(default=24, ge=0, le=168)


class SecretRotated(BaseModel):
    secret: str
    previous_secret_expires_at: datetime


class EndpointPage(BaseModel):
    data: list[EndpointOut]
    next_cursor: str | None


def _encode_cursor(e: Endpoint) -> str:
    return base64.urlsafe_b64encode(f"{e.created_at.isoformat()}|{e.id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        ts, id_ = base64.urlsafe_b64decode(cursor).decode().split("|")
        return datetime.fromisoformat(ts), uuid.UUID(id_)
    except ValueError as exc:
        raise HTTPException(400, "invalid cursor") from exc


async def _get(session: DbSession, tenant: uuid.UUID, id: uuid.UUID) -> Endpoint:
    ep = await session.scalar(
        select(Endpoint).where(
            Endpoint.id == id, Endpoint.tenant_id == tenant, Endpoint.deleted_at.is_(None)
        )
    )
    if ep is None:  # other tenants' endpoints 404 too, so ids don't leak
        raise HTTPException(404, "endpoint not found")
    return ep


@router.post("", status_code=201)
async def create_endpoint(
    body: EndpointIn, tenant: TenantId, session: DbSession
) -> EndpointCreated:
    await check_destination(body.url)
    secret = _new_secret()
    ep = Endpoint(tenant_id=tenant, secret_encrypted=encrypt(secret), **body.model_dump())
    session.add(ep)
    await session.commit()
    await session.refresh(ep)
    return EndpointCreated(**EndpointOut.model_validate(ep).model_dump(), secret=secret)


@router.get("")
async def list_endpoints(
    tenant: TenantId,
    session: DbSession,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
) -> EndpointPage:
    q = select(Endpoint).where(Endpoint.tenant_id == tenant, Endpoint.deleted_at.is_(None))
    if cursor:
        q = q.where(tuple_(Endpoint.created_at, Endpoint.id) > _decode_cursor(cursor))
    rows = list(
        await session.scalars(q.order_by(Endpoint.created_at, Endpoint.id).limit(limit + 1))
    )
    more = len(rows) > limit
    rows = rows[:limit]
    return EndpointPage(
        data=[EndpointOut.model_validate(r) for r in rows],
        next_cursor=_encode_cursor(rows[-1]) if more else None,
    )


@router.get("/{id}")
async def get_endpoint(id: uuid.UUID, tenant: TenantId, session: DbSession) -> EndpointOut:
    return EndpointOut.model_validate(await _get(session, tenant, id))


@router.patch("/{id}")
async def update_endpoint(
    id: uuid.UUID, body: EndpointPatch, tenant: TenantId, session: DbSession
) -> EndpointOut:
    ep = await _get(session, tenant, id)
    if body.url is not None:
        await check_destination(body.url)
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(ep, field, value)
    if "status" in changes:
        ep.disabled_at = func.now() if changes["status"] == "disabled" else None
    await session.commit()
    await session.refresh(ep)
    return EndpointOut.model_validate(ep)


@router.delete("/{id}", status_code=204)
async def delete_endpoint(id: uuid.UUID, tenant: TenantId, session: DbSession) -> Response:
    ep = await _get(session, tenant, id)
    ep.deleted_at = func.now()
    # Nothing will ever deliver these now; dead-letter them so they stay visible and
    # replayable instead of sitting in the queue forever.
    await session.execute(
        update(Delivery)
        .where(Delivery.endpoint_id == ep.id, Delivery.status == "pending")
        .values(status="dead_lettered")
    )
    await session.commit()
    return Response(status_code=204)


@router.post("/{id}/rotate-secret")
async def rotate_secret(
    id: uuid.UUID, tenant: TenantId, session: DbSession, body: RotateIn | None = None
) -> SecretRotated:
    """Issue a new signing secret. Until the overlap ends, deliveries carry a signature for
    both the new and the old secret, so receivers can move over at their own pace."""
    ep = await _get(session, tenant, id)
    secret = _new_secret()
    ep.previous_secret_encrypted = ep.secret_encrypted
    ep.previous_secret_expires_at = func.now() + timedelta(hours=(body or RotateIn()).overlap_hours)
    ep.secret_encrypted = encrypt(secret)
    await session.commit()
    await session.refresh(ep)
    assert ep.previous_secret_expires_at is not None
    return SecretRotated(secret=secret, previous_secret_expires_at=ep.previous_secret_expires_at)
