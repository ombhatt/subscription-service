"""let the service ask which of its users no longer have an account

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-24

Deleting someone in Supabase's dashboard removes their `auth.users` row and
nothing else. Their Stripe subscription keeps renewing, and the nightly
reconcile job keeps mirroring it into a row for a user who can no longer sign
in. To notice, the job has to know which accounts still exist -- and
`app_service` deliberately cannot read the `auth` schema (migration 0005).

So this adds one narrow window rather than widening the role:
`app_private.missing_accounts(uuid[])` takes ids the caller already holds and
returns the ones with no account. It returns ids, never rows: no email, no
metadata, nothing that was not passed in. It is SECURITY DEFINER, so it reads
`auth.users` as its owner (the migration credential), with an empty
search_path so a caller cannot redirect what it resolves.

**Why its own schema.** Supabase serves every function in `public` over
PostgREST's `/rpc`, and its default privileges grant EXECUTE there to `anon`.
`app_private` is not an exposed schema, and only `app_service` gets USAGE on it.

**Off Supabase** (plain Postgres, e.g. docker-compose) there is no `auth.users`.
The function still exists and returns NULL, which callers read as "cannot
tell" rather than "every account is missing".
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

APP_ROLE = "app_service"
SCHEMA = "app_private"
FUNCTION = f"{SCHEMA}.missing_accounts(uuid[])"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    op.execute(f"REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC")
    # plpgsql, not sql: a SQL-language body is checked at creation and would
    # fail on a database with no auth schema. This one only touches auth.users
    # after confirming it is there.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.missing_accounts(ids uuid[])
        RETURNS uuid[]
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = ''
        AS $$
        BEGIN
            IF to_regclass('auth.users') IS NULL THEN
                RETURN NULL;
            END IF;
            RETURN coalesce(
                (SELECT array_agg(i) FROM unnest(ids) AS i
                 WHERE NOT EXISTS (SELECT 1 FROM auth.users u WHERE u.id = i)),
                '{{}}'::uuid[]
            );
        END
        $$
        """
    )
    # Functions are executable by PUBLIC unless revoked.
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {APP_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION {FUNCTION} TO {APP_ROLE}")


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}")
    op.execute(f"DROP SCHEMA IF EXISTS {SCHEMA}")
