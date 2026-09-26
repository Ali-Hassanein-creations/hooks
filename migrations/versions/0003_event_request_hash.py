"""events.request_hash, to reject an Idempotency-Key reused with a different body

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    # Nullable, so existing rows need no backfill and the ALTER is instant.
    op.add_column("events", sa.Column("request_hash", sa.String(64)))


def downgrade() -> None:
    op.drop_column("events", "request_hash")
