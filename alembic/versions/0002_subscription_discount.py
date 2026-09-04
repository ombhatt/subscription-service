"""mirror the active discount onto the subscription

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # Nullable with no default: an existing subscriber simply has no discount
    # recorded until the next sync, which happens on their next webhook.
    op.add_column("subscriptions", sa.Column("discount", JSONType, nullable=True))


def downgrade() -> None:
    op.drop_column("subscriptions", "discount")
