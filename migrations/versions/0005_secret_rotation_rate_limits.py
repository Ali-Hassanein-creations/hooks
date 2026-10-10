"""secret rotation overlap on endpoints; per-tenant ingest rate-limit counters

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    # Additive and nullable: no backfill, no rewrite, safe on a live table.
    op.add_column("endpoints", sa.Column("previous_secret_encrypted", sa.LargeBinary))
    op.add_column("endpoints", sa.Column("previous_secret_expires_at", sa.DateTime(timezone=True)))
    # One row per tenant: the current one-minute window and how many events it has seen.
    op.create_table(
        "rate_limits",
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("rate_limits")
    op.drop_column("endpoints", "previous_secret_expires_at")
    op.drop_column("endpoints", "previous_secret_encrypted")
