"""close public API access to the application tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-08

Supabase serves PostgREST at https://<project>.supabase.co/rest/v1/ over the
*anon* key -- which is public by design and ships inside the frontend bundle.
Every table Alembic created landed in `public` with Row-Level Security off and,
through Supabase's default privileges, full DML granted to `anon` and
`authenticated`.

Measured before this migration was written, using the anon key from
web/.env.local:

    subscriptions        HTTP 206  READABLE  rows=6
    subscription_audit   HTTP 206  READABLE  rows=18

Reading customer records is the mild half. The grants also allowed INSERT into
`entitlement_grants`, which is the table entitlement resolution reads to decide
what a user is entitled to -- so anyone with the public key could grant
themselves any tier, including Enterprise. And TRUNCATE on all of it.

The fix is to close the door rather than to write per-user policies. This
service reaches Postgres as the `postgres` role through the session pooler and
never uses PostgREST; the frontend uses Supabase for authentication only and
never reads a table directly. Nothing legitimate goes through `anon`.

Two layers, because either alone has a gap:

* REVOKE removes today's access, but Supabase's default privileges would grant
  it again on the next table a migration creates.
* ENABLE ROW LEVEL SECURITY with *no policies* denies by default, so a
  re-granted or newly created table is still closed.

Plus ALTER DEFAULT PRIVILEGES, so the next migration's table is not born
exposed -- which is exactly what would have happened to `sales_inquiries`.

Note for later: RLS does not apply to a table's owner, which is why the service
keeps working as `postgres`. If this ever moves to the scoped, non-superuser
role the README recommends, that role needs explicit policies or BYPASSRLS --
otherwise it will be denied by the very rules added here.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Everything this service owns, plus Alembic's own bookkeeping -- `anon` could
# TRUNCATE alembic_version and leave the next deploy unable to tell which
# migrations had run.
TABLES = (
    "subscriptions",
    "processed_events",
    "usage_counters",
    "entitlement_grants",
    "subscription_audit",
    "sales_inquiries",
    "alembic_version",
)

WEB_ROLES = ("anon", "authenticated")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # SQLite has neither RLS nor these roles; the suite runs there.
    if not _is_postgres():
        return

    conn = op.get_bind()
    for table in TABLES:
        op.execute(f'ALTER TABLE IF EXISTS public."{table}" ENABLE ROW LEVEL SECURITY')

    # Guarded: a plain Postgres (the CI migrations job, a local container) has
    # no anon/authenticated roles, and REVOKE on a missing role is an error.
    for role in WEB_ROLES:
        exists = conn.exec_driver_sql(
            f"select 1 from pg_roles where rolname = '{role}'"
        ).scalar()
        if not exists:
            continue
        for table in TABLES:
            op.execute(f'REVOKE ALL ON public."{table}" FROM {role}')
        # Stops the next table created by a migration from being granted.
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role}"
        )


def downgrade() -> None:
    """Reopens the hole. Here for completeness, not because you should run it."""
    if not _is_postgres():
        return
    for table in TABLES:
        op.execute(f'ALTER TABLE IF EXISTS public."{table}" DISABLE ROW LEVEL SECURITY')
