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


def _later_migrations():
    """Migrations after 0005 that create tables lock them down themselves:
    0004 and 0005 run before those tables exist on a fresh database, and never
    run again on one already past them. Such a migration declares what it
    closed in `LOCKED_TABLES` and what it granted in `APP_ROLE_GRANTS`."""
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        if path.name > ROLE_MIGRATION.name:
            yield _load(path)


def _locked_tables() -> set[str]:
    tables = set(_load(MIGRATION).TABLES)
    for module in _later_migrations():
        tables |= set(getattr(module, "LOCKED_TABLES", ()))
    return tables


def _role_tables() -> set[str]:
    tables = set(_load(ROLE_MIGRATION).TABLES)
    for module in _later_migrations():
        tables |= set(getattr(module, "APP_ROLE_GRANTS", {}))
    return tables


def test_every_model_table_is_locked_down():
    """Add a table, lock it down in the same change -- or this fails."""
    declared = set(Base.metadata.tables)
    missing = declared - _locked_tables()
    assert not missing, (
        f"{sorted(missing)} would be readable and writable over Supabase's public "
        f"API. Lock them down in the migration that creates them and list them in "
        f"its LOCKED_TABLES (see 0008), or explain in {MIGRATION.name} why they are "
        f"safe to leave open."
    )


def test_alembics_own_bookkeeping_is_locked_too():
    """`anon` could otherwise TRUNCATE alembic_version and leave the next
    deploy unable to tell which migrations had run."""
    assert "alembic_version" in _locked_tables()


def test_the_lockdown_is_a_no_op_off_postgres():
    """The suite runs on SQLite, which has neither RLS nor these roles."""
    recorder = _RecordingOp()
    recorder.name = "sqlite"
    module = _load(MIGRATION)
    module.op = recorder
    module.upgrade()
    module.downgrade()
    assert recorder.sql == [], f"issued Postgres-only SQL on SQLite: {recorder.sql}"


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
        f"be denied on it. Grant and add a policy in the migration that creates "
        f"them, listed in its APP_ROLE_GRANTS (see 0008)."
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


class _RecordingOp:
    """Stands in for alembic's `op` as if bound to Postgres, and keeps the SQL."""

    def __init__(self):
        self.sql: list[str] = []

    def execute(self, statement):
        self.sql.append(str(statement))

    def get_bind(self):
        return self

    @property
    def dialect(self):
        return self

    name = "postgresql"

    def exec_driver_sql(self, statement):
        return self

    def scalar(self):
        return None

    def __getattr__(self, _name):  # create_table, add_column, ... are not SQL we check
        return lambda *args, **kwargs: None


def test_the_app_role_cannot_be_redirected_to_a_schema_named_after_it():
    """The default search_path is `"$user", public`, and ci_runner holds CREATE
    on the database, so it can create a schema called `app_service` and grant
    the role USAGE on it. That schema is then searched *before* `public` for
    every unqualified table the service queries -- ci_runner's tables answer
    instead of the real ones. The role's search_path has to be pinned."""
    recorder = _RecordingOp()
    for path in sorted(VERSIONS.glob("[0-9]*.py")):
        module = _load(path)
        module.op = recorder
        module.upgrade()

    pins = [
        sql.strip()
        for sql in recorder.sql
        if sql.strip().lower().startswith("alter role app_service set search_path")
    ]
    assert pins, "no migration pins app_service's search_path"
    assert pins[-1].lower() == "alter role app_service set search_path = public"


def test_later_migrations_do_what_they_declare():
    """`LOCKED_TABLES` and `APP_ROLE_GRANTS` are what the two tests above trust.
    A declaration the SQL does not carry out would pass them and leave the
    table open -- or closed to the service."""
    for module in _later_migrations():
        recorder = _RecordingOp()
        module.op = recorder
        module.upgrade()
        sql = [s.strip() for s in recorder.sql]
        for table in getattr(module, "LOCKED_TABLES", ()):
            assert f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY' in sql, (
                f"{module.__name__} lists {table} as locked but never enables RLS on it"
            )
        for table, privileges in getattr(module, "APP_ROLE_GRANTS", {}).items():
            assert f'GRANT {privileges} ON public."{table}" TO app_service' in sql
            assert any(
                s.startswith(f'CREATE POLICY app_service_full_access ON public."{table}"')
                for s in sql
            ), f"{module.__name__} grants {table} but adds no policy for app_service"


def test_the_service_cannot_rewrite_the_credit_ledger():
    """Finance's record of every credit movement. The service appends to it;
    a bug or a leaked DATABASE_URL must not be able to edit or erase it."""
    grants = _load(VERSIONS / "0008_credit_ledger.py").APP_ROLE_GRANTS
    ledger = {p.strip() for p in grants["credit_ledger"].split(",")}
    assert ledger == {"SELECT", "INSERT"}
    # Batches outlive the account they belong to: spent down, never deleted.
    assert "DELETE" not in grants["credit_batches"]
