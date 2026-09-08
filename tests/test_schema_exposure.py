"""Every application table must be closed to Supabase's public API.

Supabase serves PostgREST over the *anon* key, which is public by design and
ships in the frontend bundle. Tables created by Alembic land in `public`, and
Supabase's default privileges grant `anon` full DML on them -- so a table added
without being locked down is readable and writable by anyone with that key.

That is not hypothetical. Before migration 0004, `subscriptions` and
`subscription_audit` were both readable over the internet with the key from
web/.env.local, and `entitlement_grants` was writable -- which is the table
entitlement resolution reads, so anyone could have granted themselves any tier.

The failure mode this guards is mundane: someone adds a table in a future
migration and does not think about `anon`, because nothing makes them.
"""

from __future__ import annotations

import importlib.util
import pathlib

from app.models import Base

VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"
MIGRATION = VERSIONS / "0004_lock_down_public_schema.py"
ROLE_MIGRATION = VERSIONS / "0005_scoped_app_role.py"


def _load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _locked_tables() -> set[str]:
    return set(_load(MIGRATION).TABLES)


def _role_tables() -> set[str]:
    return set(_load(ROLE_MIGRATION).TABLES)


def test_every_model_table_is_locked_down():
    """Add a table, lock it down in the same change -- or this fails."""
    declared = set(Base.metadata.tables)
    missing = declared - _locked_tables()
    assert not missing, (
        f"{sorted(missing)} would be readable and writable over Supabase's public "
        f"API. Add them to TABLES in {MIGRATION.name}, or explain in that file why "
        f"they are safe to leave open."
    )


def test_alembics_own_bookkeeping_is_locked_too():
    """`anon` could otherwise TRUNCATE alembic_version and leave the next
    deploy unable to tell which migrations had run."""
    assert "alembic_version" in _locked_tables()


def test_the_lockdown_is_a_no_op_off_postgres():
    """The suite runs on SQLite, which has neither RLS nor these roles."""
    spec = importlib.util.spec_from_file_location("lockdown_0004b", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert hasattr(module, "_is_postgres"), "the dialect guard must exist"


# --------------------------------------------------------------------------
# the scoped role
# --------------------------------------------------------------------------


def test_the_app_role_can_reach_every_table_the_service_uses():
    """RLS denies anyone who is not the table owner, and the running service is
    no longer the owner. A table without a policy for `app_service` is a table
    the service cannot read -- discovered in production, at the worst moment.

    This is the cost of choosing per-table policies over BYPASSRLS, and this
    test is what makes that cost a failing check rather than an outage.
    """
    declared = set(Base.metadata.tables)
    missing = declared - _role_tables()
    assert not missing, (
        f"{sorted(missing)} has no policy for the app role, so the service will "
        f"be denied on it. Add them to TABLES in {ROLE_MIGRATION.name}."
    )


def test_the_app_role_is_not_granted_alembics_bookkeeping():
    """Migrations run as the admin credential. The running service has no
    business rewriting the record of which migrations have run."""
    assert "alembic_version" not in _role_tables()


def test_the_role_migration_carries_no_credential():
    """A password in a migration is a password in the repository.

    Checks the SQL rather than the file: the docstring discusses passwords at
    length, so a naive substring search over the whole source would pass while
    proving nothing.
    """
    source = ROLE_MIGRATION.read_text()
    sql_markers = ("op.execute", "create role", "alter role", "grant ", "create policy")
    sql_lines = [
        line for line in source.splitlines()
        if any(marker in line.lower() for marker in sql_markers)
    ]
    offenders = [line.strip() for line in sql_lines if "password" in line.lower()]
    assert not offenders, f"SQL sets a credential: {offenders}"
    assert "nologin" in source.lower(), "the role must be created without login"
