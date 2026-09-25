"""tenants, api keys, endpoints

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision = "0001"
down_revision = None


def _id() -> sa.Column[str]:
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _now(name: str) -> sa.Column[str]:
    return sa.Column(
        name, sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def _tenant() -> sa.Column[str]:
    return sa.Column("tenant_id", UUID, sa.ForeignKey("tenants.id"), nullable=False, index=True)


def upgrade() -> None:
    op.create_table(
        "tenants",
        _id(),
        sa.Column("name", sa.String(200), nullable=False),
        _now("created_at"),
    )
    op.create_table(
        "api_keys",
        _id(),
        _tenant(),
        sa.Column("prefix", sa.String(16), nullable=False, unique=True),
        sa.Column("key_hash", sa.String(64), nullable=False),
        _now("created_at"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "endpoints",
        _id(),
        _tenant(),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("secret_encrypted", sa.LargeBinary, nullable=False),
        sa.Column("event_types", ARRAY(sa.Text), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="enabled"),
        _now("created_at"),
        sa.Column("disabled_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('enabled', 'disabled')", name="ck_endpoints_status"),
    )
    op.create_index("ix_endpoints_tenant_cursor", "endpoints", ["tenant_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_table("endpoints")
    op.drop_table("api_keys")
    op.drop_table("tenants")
