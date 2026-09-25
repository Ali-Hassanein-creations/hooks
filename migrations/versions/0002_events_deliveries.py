"""events and deliveries (the outbox)

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0002"
down_revision = "0001"


def _ts(name: str, default: bool = True) -> sa.Column[str]:
    if default:
        return sa.Column(
            name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        )
    return sa.Column(name, sa.DateTime(timezone=True))


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(200), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        _ts("created_at"),
        # Also serves as the tenant_id index for lookups.
        sa.UniqueConstraint("tenant_id", "idempotency_key"),
    )
    op.create_table(
        "deliveries",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("event_id", UUID, sa.ForeignKey("events.id"), nullable=False, index=True),
        sa.Column("endpoint_id", UUID, sa.ForeignKey("endpoints.id"), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        _ts("next_attempt_at"),
        _ts("locked_until", default=False),
        _ts("created_at"),
        sa.UniqueConstraint("event_id", "endpoint_id"),
        sa.CheckConstraint(
            "status IN ('pending', 'in_flight', 'succeeded', 'failed', 'dead_lettered')",
            name="ck_deliveries_status",
        ),
    )
    # Partial: the worker's claim query only ever wants pending rows, so the index stays
    # small however many succeeded rows accumulate.
    op.create_index(
        "ix_deliveries_claimable",
        "deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("deliveries")
    op.drop_table("events")
