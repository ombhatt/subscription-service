from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import get_settings
from app.db import engine_kwargs
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    """The credential migrations run as.

    Falls back to DATABASE_URL, so nothing changes for a single-credential
    setup. Where the two differ, this is the admin one -- the running service
    connects as a role that cannot ALTER anything.
    """
    return get_settings().alembic_url


# Where the version table and every unqualified name resolve, on Postgres.
#
# The migration credential keeps the default search_path, `"$user", public`, so
# a schema named after it is searched first. ci_runner holds CREATE on the
# database (scripts/ci_db_role.sql) and can create a schema called `postgres`
# with its own `alembic_version`: Alembic then reads that, and re-runs or skips
# migrations -- including the one that closed the same hole for app_service
# (0007) -- and an unqualified CREATE TABLE lands in ci_runner's schema.
#
# The fix is pinning the search_path for the migration transaction: it is set
# before Alembic reads its version table, so it covers both. The version table
# is also named by schema, which is redundant today and stays as a guard in
# case the SET is ever moved. Neither touches the Supabase-managed role.
SCHEMA = "public"


def _is_postgres(url: str) -> bool:
    return url.startswith("postgresql")


def run_migrations_offline() -> None:
    url = _url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=SCHEMA if _is_postgres(url) else None,
    )
    with context.begin_transaction():
        if _is_postgres(url):
            context.execute(f"SET LOCAL search_path TO {SCHEMA}")
        context.run_migrations()


def do_run_migrations(connection) -> None:
    postgres = connection.dialect.name == "postgresql"
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA if postgres else None,
    )
    with context.begin_transaction():
        if postgres:
            # LOCAL: lasts for this transaction only, so it holds behind a
            # transaction pooler too. Migrations run in one transaction.
            context.execute(f"SET LOCAL search_path TO {SCHEMA}")
        context.run_migrations()


async def run_async_migrations() -> None:
    # Same connection handling as the app: a transaction pooler breaks prepared
    # statements, and a migration is exactly the wrong time to discover that.
    engine = create_async_engine(_url(), **engine_kwargs(_url()))
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
