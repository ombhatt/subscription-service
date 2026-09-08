"""record enterprise sales inquiries

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-07
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A new table only -- nothing existing is touched, so this is safe to run
    # while the previous version is still serving, which is what the rolling
    # deploy in DEPLOY.md requires.
    op.create_table(
        "sales_inquiries",
        sa.Column("id", sa.String(36), primary_key=True),
        # Nullable: the pricing page is public, and a visitor evaluating before
        # signing up is exactly the lead worth capturing.
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("company", sa.String(200), nullable=True),
        sa.Column("seats", sa.Integer(), nullable=True),
        sa.Column("message", sa.String(2000), nullable=True),
        sa.Column("source", sa.String(32), nullable=False, server_default="pricing_page"),
        sa.Column("current_tier", sa.String(32), nullable=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sales_inquiries_user_id", "sales_inquiries", ["user_id"])
    op.create_index("ix_sales_inquiry_created", "sales_inquiries", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_sales_inquiry_created", table_name="sales_inquiries")
    op.drop_index("ix_sales_inquiries_user_id", table_name="sales_inquiries")
    op.drop_table("sales_inquiries")
