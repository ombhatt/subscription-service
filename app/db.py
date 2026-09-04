from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

# Supabase's transaction pooler listens here. Session mode uses 5432, like a
# direct connection.
TRANSACTION_POOLER_PORT = ":6543"


def engine_kwargs(url: str) -> dict[str, Any]:
    """Connection settings that depend on how we reach the database.

    Supabase offers three routes and they are not interchangeable:

    * **Direct** (`db.<ref>.supabase.co:5432`) — on projects created since 2024
      this hostname resolves to IPv6 only, so it fails from any network without
      IPv6 egress. GitHub Actions runners are one such network.
    * **Session pooler** (`…pooler.supabase.com:5432`) — reachable over IPv4 and
      holds a backend connection for the whole session, so prepared statements
      work normally. This is the one to use for a long-running server.
    * **Transaction pooler** (`…pooler.supabase.com:6543`) — reachable over IPv4
      and returns the backend connection after every transaction, which means
      prepared statements cannot be relied on. asyncpg uses them by default, so
      it has to be told otherwise or queries fail with
      `prepared statement "__asyncpg_stmt_1__" already exists`.

    Only the third needs special handling, and it is handled here rather than
    left as a footnote someone discovers in production.
    """
    if not url.startswith("postgresql"):
        # SQLite (tests, and local development before Postgres) wants none of this.
        return {}

    settings = get_settings()
    kwargs: dict[str, Any] = {
        "pool_pre_ping": True,
        # Without this a statement waits forever. asyncpg applies it per query,
        # so a wedged connection surfaces as an error rather than a hung worker.
        "connect_args": {
            "command_timeout": settings.db_command_timeout_seconds,
            "timeout": settings.db_command_timeout_seconds,
        },
    }

    if TRANSACTION_POOLER_PORT in url:
        kwargs["connect_args"].update(
            {
                # asyncpg's own statement cache, which a transaction pooler breaks.
                "statement_cache_size": 0,
                # Even with caching off, asyncpg names statements sequentially and
                # two pooled sessions can collide. Unique names remove that.
                "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
            }
        )
        # SQLAlchemy keeps a cache of its own on top of asyncpg's.
        kwargs["prepared_statement_cache_size"] = 0

    return kwargs


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        url = get_settings().database_url
        _engine = create_async_engine(url, **engine_kwargs(url))
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. One session per request, rolled back on exception."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
