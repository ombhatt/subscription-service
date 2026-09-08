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

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "alembic" / "versions" / "0004_lock_down_public_schema.py"
)


def _locked_tables() -> set[str]:
    spec = importlib.util.spec_from_file_location("lockdown_0004", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return set(module.TABLES)


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
