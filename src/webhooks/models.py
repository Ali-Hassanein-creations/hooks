import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Statuses are TEXT + CHECK rather than Postgres enums: adding a value to a CHECK is a
# plain migration, while ALTER TYPE ... ADD VALUE can't run inside a transaction.


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True)}


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))


def _now() -> Mapped[datetime]:
    return mapped_column(server_default=text("now()"))


def _tenant() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("tenants.id"), index=True)


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _now()


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    prefix: Mapped[str] = mapped_column(String(16), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _now()
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (Index("ix_endpoints_tenant_cursor", "tenant_id", "created_at", "id"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    url: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(Text))
    status: Mapped[str] = mapped_column(String(16), server_default="enabled")
    created_at: Mapped[datetime] = _now()
    disabled_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    # SHA-256 of the request body; NULL for events stored before migration 0003.
    request_hash: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _now()


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("event_id", "endpoint_id"),
        # The worker's claim query only ever looks at pending rows; indexing just those
        # keeps the index small no matter how many succeeded rows pile up.
        Index(
            "ix_deliveries_claimable",
            "next_attempt_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("events.id"), index=True)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("endpoints.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime] = _now()
    locked_until: Mapped[datetime | None]
    created_at: Mapped[datetime] = _now()
