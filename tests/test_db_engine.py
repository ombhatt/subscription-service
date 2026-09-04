"""How we connect, which depends on which Supabase route the URL points at.

Getting this wrong does not fail cleanly. A transaction pooler with prepared
statements left on works fine until two pooled sessions collide, and then
throws `prepared statement "__asyncpg_stmt_1__" already exists` under exactly
the load you least want it under.
"""

from __future__ import annotations

from app.db import engine_kwargs

DIRECT = "postgresql+asyncpg://u:p@db.ref.supabase.co:5432/postgres"
SESSION_POOLER = "postgresql+asyncpg://u:p@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
TRANSACTION_POOLER = "postgresql+asyncpg://u:p@aws-0-us-east-1.pooler.supabase.com:6543/postgres"


def test_sqlite_gets_no_postgres_settings():
    """The test suite and local SQLite want none of this."""
    assert engine_kwargs("sqlite+aiosqlite:///./dev.sqlite3") == {}
    assert engine_kwargs("sqlite+aiosqlite://") == {}


def test_a_direct_connection_keeps_prepared_statements():
    kwargs = engine_kwargs(DIRECT)
    assert kwargs["pool_pre_ping"] is True
    assert "prepared_statement_cache_size" not in kwargs


def test_the_session_pooler_keeps_prepared_statements():
    """Session mode holds the backend connection, so caching is safe and worth
    keeping -- this is the route to prefer for a long-running server."""
    kwargs = engine_kwargs(SESSION_POOLER)
    assert kwargs["pool_pre_ping"] is True
    assert "connect_args" not in kwargs


def test_the_transaction_pooler_disables_prepared_statements():
    kwargs = engine_kwargs(TRANSACTION_POOLER)
    assert kwargs["prepared_statement_cache_size"] == 0
    assert kwargs["connect_args"]["statement_cache_size"] == 0


def test_transaction_pooler_statement_names_are_unique():
    """Even with caching off, asyncpg names statements sequentially and two
    pooled sessions can collide on the same name."""
    name_func = engine_kwargs(TRANSACTION_POOLER)["connect_args"][
        "prepared_statement_name_func"
    ]
    names = {name_func() for _ in range(100)}
    assert len(names) == 100
    assert all(name.startswith("__asyncpg_") for name in names)
