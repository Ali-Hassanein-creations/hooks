import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from webhooks.db import get_session
from webhooks.models import ApiKey

DbSession = Annotated[AsyncSession, Depends(get_session)]


def _hash(key: str) -> str:
    # Keys carry 256 bits of randomness, so a fast hash is fine: slow KDFs only
    # help against guessable secrets like passwords.
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key(tenant_id: uuid.UUID) -> tuple[ApiKey, str]:
    """Returns the row to insert and the plaintext key, which is shown once."""
    prefix = secrets.token_hex(4)
    key = f"whk_{prefix}_{secrets.token_urlsafe(32)}"
    return ApiKey(tenant_id=tenant_id, prefix=prefix, key_hash=_hash(key)), key


async def current_tenant(request: Request, session: DbSession) -> uuid.UUID:
    unauthorized = HTTPException(401, "invalid API key", {"WWW-Authenticate": "Bearer"})
    scheme, _, key = request.headers.get("Authorization", "").partition(" ")
    parts = key.split("_", 2)
    if scheme.lower() != "bearer" or len(parts) != 3 or parts[0] != "whk":
        raise unauthorized
    row = await session.scalar(
        select(ApiKey).where(ApiKey.prefix == parts[1], ApiKey.revoked_at.is_(None))
    )
    if row is None or not hmac.compare_digest(row.key_hash, _hash(key)):
        raise unauthorized
    # At most one write per key per minute, so a busy key's row doesn't become a hot spot.
    await session.execute(
        update(ApiKey)
        .where(
            ApiKey.id == row.id,
            or_(
                ApiKey.last_used_at.is_(None),
                ApiKey.last_used_at < func.now() - timedelta(minutes=1),
            ),
        )
        .values(last_used_at=func.now())
    )
    await session.commit()
    return row.tenant_id


TenantId = Annotated[uuid.UUID, Depends(current_tenant)]
