"""Cache + atomic counters.

Two consumers: cached entitlement sets (read path, must survive a database
outage) and quota counters (must be atomic across workers, which is why this is
Redis and not a dict in the process).

The in-memory backend exists so the test suite and a bare `uvicorn` run work
without Redis. It is per-process and therefore *not* correct under more than one
worker -- `REDIS_URL` must be set anywhere that matters.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol

from app.config import get_settings


class CacheBackend(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def incr(self, key: str, ttl: int) -> int: ...
    async def get_int(self, key: str) -> int: ...
    async def close(self) -> None: ...


class InMemoryBackend:
    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}

    def _live(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at <= time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._data[key] = (value, time.monotonic() + ttl)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl: int) -> int:
        current = self._live(key)
        if current is None:
            self._data[key] = ("1", time.monotonic() + ttl)
            return 1
        value = int(current) + 1
        self._data[key] = (str(value), self._data[key][1])
        return value

    async def get_int(self, key: str) -> int:
        return int(self._live(key) or 0)

    async def close(self) -> None:
        self._data.clear()


class RedisBackend:
    def __init__(self, url: str) -> None:
        from redis.asyncio import from_url

        settings = get_settings()
        # The cache sits in front of every request. If Redis stops answering it
        # must fail quickly so the read path can fall back, rather than turning
        # a cache problem into a latency problem for everyone.
        self._redis = from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=settings.redis_timeout_seconds,
            socket_connect_timeout=settings.redis_timeout_seconds,
        )

    async def get(self, key: str) -> str | None:
        return await self._redis.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self._redis.set(key, value, ex=ttl)

    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def incr(self, key: str, ttl: int) -> int:
        value = await self._redis.incr(key)
        # Only the increment that created the key needs to set the window's TTL;
        # the extra TTL check repairs a key left without one by a crashed worker.
        if value == 1 or await self._redis.ttl(key) < 0:
            await self._redis.expire(key, ttl)
        return int(value)

    async def get_int(self, key: str) -> int:
        return int(await self._redis.get(key) or 0)

    async def close(self) -> None:
        await self._redis.aclose()


_backend: CacheBackend | None = None


def get_cache() -> CacheBackend:
    global _backend
    if _backend is None:
        url = get_settings().redis_url
        _backend = RedisBackend(url) if url else InMemoryBackend()
    return _backend


def set_cache(backend: CacheBackend | None) -> None:
    """Test seam."""
    global _backend
    _backend = backend


async def close_cache() -> None:
    global _backend
    if _backend is not None:
        await _backend.close()
    _backend = None


async def get_json(key: str) -> Any | None:
    raw = await get_cache().get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def set_json(key: str, value: Any, ttl: int) -> None:
    await get_cache().set(key, json.dumps(value), ttl)
