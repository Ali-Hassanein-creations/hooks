"""delivery_attempts audit table; next_attempt_at becomes the worker lease

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0004"
down_revision = "0003"

OLD_STATUSES = "'pending', 'in_flight', 'succeeded', 'failed', 'dead_lettered'"
NEW_STATUSES = "'pending', 'succeeded', 'dead_lettered'"


def upgrade() -> None:
    op.create_table(
        "delivery_attempts",
        sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("delivery_id", UUID, sa.ForeignKey("deliveries.id"), nullable=False),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("request_headers", JSONB, nullable=False),
        sa.Column("response_status", sa.Integer),
        sa.Column("response_body", sa.Text),
        sa.Column("duration_ms", sa.Integer, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Also serves as the delivery_id index.
        sa.UniqueConstraint("delivery_id", "attempt_number"),
    )
    # Claiming pushes next_attempt_at forward as a lease, so a separate lock column and an
    # in_flight state are unnecessary: a crashed worker's rows simply become due again.
    op.drop_column("deliveries", "locked_until")
    op.drop_constraint("ck_deliveries_status", "deliveries", type_="check")
    op.create_check_constraint("ck_deliveries_status", "deliveries", f"status IN ({NEW_STATUSES})")


def downgrade() -> None:
    op.drop_constraint("ck_deliveries_status", "deliveries", type_="check")
    op.create_check_constraint("ck_deliveries_status", "deliveries", f"status IN ({OLD_STATUSES})")
    op.add_column("deliveries", sa.Column("locked_until", sa.DateTime(timezone=True)))
    op.drop_table("delivery_attempts")
