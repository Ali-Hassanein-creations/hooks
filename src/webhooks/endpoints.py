import base64
import secrets
import uuid
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import func, select, tuple_

from webhooks.auth import DbSession, TenantId
from webhooks.db import encrypt
from webhooks.models import Endpoint

router = APIRouter(prefix="/v1/endpoints", tags=["endpoints"])

EventTypes = list[str]


def check_url(url: str) -> str:
    # ponytail: scheme/port only; the week-4 SSRF guard (resolve + IP blocklist) plugs in here
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("url must be http(s) with a host")
    if parts.port not in (None, 80, 443):
        raise ValueError("only ports 80 and 443 are allowed")
    return url


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
    secret = f"whsec_{secrets.token_urlsafe(32)}"
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
    await session.commit()
    return Response(status_code=204)
