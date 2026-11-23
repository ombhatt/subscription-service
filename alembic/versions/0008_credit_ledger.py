"""prepaid credits: batches and an append-only ledger

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-23

Two new tables (docs/adr/0001-prepaid-credits.md):

* `credit_batches` -- one row per grant or purchase, with what is left of it
  and when it expires. Expiry is read-time; nothing here schedules a job.
* `credit_ledger` -- every movement of credits, signed, with its cause.

Only new tables, so this is safe while the previous version is serving.

**Locked down here, not by editing 0004 and 0005.** Those migrations run, on a
fresh database, before these tables exist: 0005's GRANT on a missing table is
an error, and on a database already past them they never run again. So this
migration closes the tables itself, the same two layers 0004 adds (RLS on,
nothing for `anon`/`authenticated`) and the same per-table policy 0005 gives
`app_service`. `LOCKED_TABLES` and `APP_ROLE_GRANTS` are read by
tests/test_schema_exposure.py alongside 0004's and 0005's `TABLES`.

**The ledger is append-only for the service.** `app_service` gets SELECT and
INSERT on `credit_ledger` and nothing else, so a bug or a leaked DATABASE_URL
cannot rewrite finance's record. Batches get UPDATE (spending decrements
`remaining`) but not DELETE: they outlive the account they belong to.
"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

APP_ROLE = "app_service"
WEB_ROLES = ("anon", "authenticated")
POLICY = "app_service_full_access"

LOCKED_TABLES = ("credit_batches", "credit_ledger")

APP_ROLE_GRANTS = {
    "credit_batches": "SELECT, INSERT, UPDATE",
    "credit_ledger": "SELECT, INSERT",
}


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "credit_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        # No foreign key, like `subscriptions`: the account lives in Supabase.
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("pack_id", sa.String(32), nullable=True),
        sa.Column("credits_granted", sa.Integer(), nullable=False),
        sa.Column("remaining", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stripe_checkout_session_id", sa.String(255), nullable=True),
        sa.Column("stripe_payment_intent_id", sa.String(255), nullable=True),
        sa.Column("amount_paid", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("credits_granted > 0", name="ck_credit_batches_granted_positive"),
        sa.CheckConstraint(
            "remaining >= 0 AND remaining <= credits_granted",
            name="ck_credit_batches_remaining_in_range",
        ),
        sa.CheckConstraint("source IN ('purchase', 'admin')", name="ck_credit_batches_source"),
        sa.UniqueConstraint(
            "stripe_checkout_session_id", name="uq_credit_batches_stripe_checkout_session_id"
        ),
    )
    op.create_index(
        "ix_credit_batches_user_expires", "credit_batches", ["user_id", "expires_at"]
    )
    op.create_index(
        "ix_credit_batches_stripe_payment_intent_id",
        "credit_batches",
        ["stripe_payment_intent_id"],
    )

    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("batch_id", sa.String(36), sa.ForeignKey("credit_batches.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("reference", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("amount <> 0", name="ck_credit_ledger_amount_nonzero"),
        sa.CheckConstraint(
            "kind IN ('purchase', 'spend', 'refund', 'dispute', 'admin', 'expire')",
            name="ck_credit_ledger_kind",
        ),
    )
    op.create_index("ix_credit_ledger_batch_id", "credit_ledger", ["batch_id"])
    op.create_index("ix_credit_ledger_user_created", "credit_ledger", ["user_id", "created_at"])

    if not _is_postgres():
        return

    conn = op.get_bind()
    for table in LOCKED_TABLES:
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
    for role in WEB_ROLES:
        exists = conn.exec_driver_sql(
            f"select 1 from pg_roles where rolname = '{role}'"
        ).scalar()
        if exists:
            for table in LOCKED_TABLES:
                op.execute(f'REVOKE ALL ON public."{table}" FROM {role}')

    for table, privileges in APP_ROLE_GRANTS.items():
        op.execute(f'GRANT {privileges} ON public."{table}" TO {APP_ROLE}')
        op.execute(f'DROP POLICY IF EXISTS {POLICY} ON public."{table}"')
        op.execute(
            f'CREATE POLICY {POLICY} ON public."{table}" '
            f"FOR ALL TO {APP_ROLE} USING (true) WITH CHECK (true)"
        )


def downgrade() -> None:
    # Dropping a table takes its policies and grants with it.
    op.drop_index("ix_credit_ledger_user_created", table_name="credit_ledger")
    op.drop_index("ix_credit_ledger_batch_id", table_name="credit_ledger")
    op.drop_table("credit_ledger")
    op.drop_index("ix_credit_batches_stripe_payment_intent_id", table_name="credit_batches")
    op.drop_index("ix_credit_batches_user_expires", table_name="credit_batches")
    op.drop_table("credit_batches")
