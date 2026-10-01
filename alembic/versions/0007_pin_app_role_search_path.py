"""pin the scoped role's search_path to public

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-29

Every query the service sends names its tables unqualified (`subscriptions`,
`entitlement_grants`, ...), so Postgres resolves them through the role's
search_path. The default is `"$user", public`: a schema named after the role,
if one exists and the role has USAGE on it, is searched *before* `public`.

Nothing in this service creates an `app_service` schema. But `ci_runner`
(scripts/ci_db_role.sql) holds CREATE on the database, so it may create a
schema with any name -- including `app_service` -- and, as its owner, grant
`app_service` USAGE on it. From then on every unqualified read and write the
service makes lands in tables the CI credential controls. Measured on
Postgres 16, as ci_runner:

    create schema app_service;
    grant usage on schema app_service to app_service;
    create table app_service.entitlement_grants (...);   -- plus a grant row
    grant all on app_service.entitlement_grants to app_service;

and then, as app_service, `select ... from entitlement_grants` returned only
the row ci_runner inserted. So code running in CI -- any same-repository pull
request, before review, or a compromised dependency -- could grant any user
any tier, and divert webhook writes away from `public`, while the role that
"cannot read or write public" never touches `public` at all.

Pinning the role's search_path to `public` removes the `"$user"` lookup. It
is a role setting, so the server applies it at session start through any
pooler, and it only affects new sessions: safe while the previous version is
still serving.
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

APP_ROLE = "app_service"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    op.execute(f"ALTER ROLE {APP_ROLE} SET search_path = public")


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(f"ALTER ROLE {APP_ROLE} RESET search_path")
