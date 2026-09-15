"""Drop integration-test schemas left behind by interrupted runs.

Against Postgres, every test in integration/ builds its tables in a fresh
schema and drops it at teardown. A cancelled or timed-out CI job kills the
process before teardown runs, and the schema -- tables, indexes, rows -- stays
in the shared Supabase project until something removes it.

Safe by construction, because that project also holds the service's real data:

* Only schemas **owned by the connecting role**. CI connects as `ci_runner`,
  which owns nothing but the schemas it created.
* Only names this suite generates, `ci_<unix epoch>_<8 hex>`. `public`, `auth`
  and anything created by hand are never candidates.
* Only once older than a cutoff, so a concurrent run -- the nightly, or another
  pull request -- is not still using them.

    python -m scripts.cleanup_ci_schemas --older-than 60
    python -m scripts.cleanup_ci_schemas --dry-run

Naming and parsing live together here so the fixture that creates schemas and
the job that removes them cannot drift apart.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
import uuid
from collections.abc import Iterable

PREFIX = "ci_"
_NAME = re.compile(rf"^{PREFIX}(\d{{10}})_[0-9a-f]{{8}}$")


def new_schema_name(now: float | None = None) -> str:
    """A schema name this module will later recognise, and date."""
    return f"{PREFIX}{int(time.time() if now is None else now)}_{uuid.uuid4().hex[:8]}"


def stale(names: Iterable[str], *, now: float, older_than_minutes: int) -> list[str]:
    """The generated schema names created more than `older_than_minutes` ago.

    Anything that does not match the generated pattern is ignored, however it
    is named -- including `ci_`-prefixed schemas someone made by hand.
    """
    cutoff = now - older_than_minutes * 60
    out = []
    for name in names:
        match = _NAME.match(name)
        if match and int(match.group(1)) < cutoff:
            out.append(name)
    return sorted(out)


async def _run(url: str, *, older_than: int, dry_run: bool) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db import engine_kwargs

    engine = create_async_engine(url, poolclass=NullPool, **engine_kwargs(url))
    try:
        async with engine.begin() as conn:
            rows = await conn.exec_driver_sql(
                "select nspname from pg_namespace "
                "where nspowner = (select oid from pg_roles where rolname = current_user) "
                "and nspname like 'ci\\_%'"
            )
            owned = [row[0] for row in rows]
            doomed = stale(owned, now=time.time(), older_than_minutes=older_than)
            for name in doomed:
                if dry_run:
                    print(f"  would drop {name}")
                else:
                    await conn.exec_driver_sql(f'drop schema if exists "{name}" cascade')
                    print(f"  dropped {name}")
    finally:
        await engine.dispose()

    verb = "to drop" if dry_run else "dropped"
    kept = len(owned) - len(doomed)
    print(f"\n{len(doomed)} {verb}, {kept} left alone, {len(owned)} owned")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--older-than",
        type=int,
        default=60,
        help="minutes; skip schemas newer than this so a running job is left alone",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    url = os.environ.get("INTEGRATION_DATABASE_URL", "")
    if not url:
        print("INTEGRATION_DATABASE_URL is not set; nothing to clean", file=sys.stderr)
        return 1
    return asyncio.run(_run(url, older_than=args.older_than, dry_run=args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
