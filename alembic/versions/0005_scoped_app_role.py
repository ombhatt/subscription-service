"""give the running service a role that can only touch its own tables

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-08

Until now the service connected as Supabase's `postgres` role. Measured, that
credential is not a true superuser but has CREATEROLE, CREATEDB and BYPASSRLS,
and can read `auth.users` -- 23 tables in the auth schema -- plus `storage` and
`vault`. So a leaked DATABASE_URL did not expose billing data; it exposed every
user account in the project.

This creates `app_service`: login-capable, DML on this service's six tables and
nothing else. Verified on a throwaway role before writing this -- a fresh role
has no USAGE on auth, storage or vault, so the isolation is the default rather
than something to remember.

Two decisions worth the words:

**Policies, not BYPASSRLS.** BYPASSRLS is available here and would have been one
line. It is also a role *attribute*: it applies to every table in the database,
including ones added years from now by someone who never read this file. A
permissive policy per table is more typing and strictly narrower, and a table
added later without one fails closed -- which is the direction you want to fail.
`tests/test_schema_exposure.py` is what turns that failure into a test rather
than an outage.

**No password here.** The role is created NOLOGIN with no password; an operator
grants LOGIN and sets a secret separately (see DEPLOY.md). A credential in a
migration is a credential in the repository.

`alembic_version` is deliberately not granted: migrations run as the admin
credential (MIGRATION_DATABASE_URL), and the running service has no business
rewriting the record of which migrations have run.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP_ROLE = "app_service"

# The tables the running service actually reads and writes. Not alembic_version.
TABLES = (
    "subscriptions",
    "processed_events",
    "usage_counters",
    "entitlement_grants",
    "subscription_audit",
    "sales_inquiries",
)

POLICY = "app_service_full_access"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # Idempotent: re-running a migration against a cluster where the role
    # already exists should not be an error.
    op.execute(
        f"""
        do $$ begin
            create role {APP_ROLE} nologin;
        exception when duplicate_object then null;
        end $$;
        """
    )

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    for table in TABLES:
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON public."{table}" TO {APP_ROLE}')
        # RLS is on with no policies (migration 0004), which denies everyone who
        # is not the table owner. This is what lets the scoped role through, and
        # only for these tables.
        op.execute(f'DROP POLICY IF EXISTS {POLICY} ON public."{table}"')
        op.execute(
            f'CREATE POLICY {POLICY} ON public."{table}" '
            f"FOR ALL TO {APP_ROLE} USING (true) WITH CHECK (true)"
        )


def downgrade() -> None:
    if not _is_postgres():
        return
    for table in TABLES:
        op.execute(f'DROP POLICY IF EXISTS {POLICY} ON public."{table}"')
        op.execute(f'REVOKE ALL ON public."{table}" FROM {APP_ROLE}')
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    # The role itself is left in place: dropping it would break any service
    # still holding a connection string that names it.
