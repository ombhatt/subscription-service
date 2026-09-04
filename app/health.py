"""Liveness and readiness, which are not the same question.

Liveness asks "is this process wedged, should the platform restart it". It must
not touch a dependency: if the database goes down and the liveness probe checks
the database, every instance fails its probe and the platform restarts all of
them, turning a recoverable outage into a crash loop.

Readiness asks "should this instance receive traffic". That one does check
dependencies -- but only the ones whose absence makes the instance unable to
answer at all.

The distinction that matters here is Postgres versus Redis:

* Without Postgres nothing works. Not ready, 503, take it out of rotation.
* Without Redis the service degrades but still answers -- entitlements fall
  back to the database. And Redis is *shared*, so failing readiness on it would
  pull every instance out of the load balancer at once and convert a cache
  problem into a total outage. So: report it, stay in rotation.

That is what `degraded` means in the payload below, and why it is still a 200.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from sqlalchemy import text

from app.cache import get_cache
from app.config import get_settings
from app.db import get_sessionmaker

log = logging.getLogger(__name__)


async def _timed(name: str, coro, timeout: float) -> dict[str, Any]:
    """Run one check under its own deadline.

    A probe that can hang is worse than no probe: the platform's own probe
    timeout fires, the instance is marked down, and the reason is invisible.
    """
    started = time.monotonic()
    try:
        await asyncio.wait_for(coro, timeout=timeout)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return {"name": name, "ok": True, "latency_ms": elapsed_ms}
    except TimeoutError:
        return {"name": name, "ok": False, "error": f"timed out after {timeout}s"}
    except Exception as exc:  # the reason belongs in the payload, not a 500
        return {"name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


async def _check_database() -> None:
    async with get_sessionmaker()() as session:
        await session.execute(text("select 1"))


async def _check_cache() -> None:
    # Exercises the real round trip rather than trusting a connection object
    # that may have been created lazily and never used.
    await get_cache().set("healthz:probe", "1", 10)
    if await get_cache().get("healthz:probe") != "1":
        raise RuntimeError("value did not survive a set/get round trip")


async def readiness() -> tuple[int, dict[str, Any]]:
    """(http status, payload). See the module docstring for why Redis is not fatal."""
    settings = get_settings()

    database, cache = await asyncio.gather(
        _timed("database", _check_database(), settings.db_command_timeout_seconds),
        # +1s so a Redis client that is itself timing out gets to report that
        # rather than being cut off by the outer deadline first.
        _timed("cache", _check_cache(), settings.redis_timeout_seconds + 1),
    )

    if not database["ok"]:
        status, code = 503, "unready"
    elif not cache["ok"]:
        status, code = 200, "degraded"
    else:
        status, code = 200, "ok"

    if status != 200 or code == "degraded":
        log.warning(
            "readiness %s", code,
            extra={"context": {"event": "health.readiness", "status": code,
                               "database": database, "cache": cache}},
        )

    return status, {
        "status": code,
        "environment": settings.environment,
        "checks": [database, cache],
    }
