"""Probes that tell the truth about dependencies.

The old /healthz returned {"status": "ok"} with the database on fire, so any
platform wired to it would have kept routing traffic to an instance that could
not answer a single request. These tests pin the distinction between the two
probes, and the deliberate asymmetry between Postgres and Redis.
"""

from __future__ import annotations

import asyncio

import pytest

from app import health


class ExplodingSessionmaker:
    """Stands in for a database that is refusing connections."""

    def __call__(self):
        return self

    async def __aenter__(self):
        raise OSError("connection refused")

    async def __aexit__(self, *exc):
        return False


class HangingSessionmaker:
    def __call__(self):
        return self

    async def __aenter__(self):
        await asyncio.sleep(30)

    async def __aexit__(self, *exc):
        return False


class BrokenCache:
    async def set(self, *a, **kw):
        raise ConnectionError("Error 111 connecting to redis:6379")

    async def get(self, *a, **kw):
        raise ConnectionError("Error 111 connecting to redis:6379")


# --------------------------------------------------------------------------
# liveness must not depend on anything
# --------------------------------------------------------------------------


async def test_liveness_ignores_a_dead_database(client, monkeypatch):
    """Otherwise a database blip restarts every instance at once."""
    monkeypatch.setattr(health, "get_sessionmaker", ExplodingSessionmaker())
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# --------------------------------------------------------------------------
# readiness
# --------------------------------------------------------------------------


async def test_ready_when_both_dependencies_answer(client):
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert {check["name"] for check in body["checks"]} == {"database", "cache"}
    assert all(check["ok"] for check in body["checks"])
    assert all("latency_ms" in check for check in body["checks"])


async def test_a_dead_database_takes_the_instance_out_of_rotation(client, monkeypatch):
    monkeypatch.setattr(health, "get_sessionmaker", ExplodingSessionmaker())
    response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unready"
    database = next(c for c in body["checks"] if c["name"] == "database")
    assert "connection refused" in database["error"], "the payload must name the cause"


async def test_a_dead_cache_is_degraded_not_unready(client, monkeypatch):
    """Redis is shared. Failing readiness on it would pull every instance out
    of the load balancer simultaneously -- a cache outage becoming a total one."""
    monkeypatch.setattr(health, "get_cache", lambda: BrokenCache())
    response = await client.get("/readyz")
    assert response.status_code == 200, "still able to serve; entitlements fall back to the db"
    body = response.json()
    assert body["status"] == "degraded"
    cache = next(c for c in body["checks"] if c["name"] == "cache")
    assert cache["ok"] is False


async def test_a_hanging_dependency_is_bounded(client, monkeypatch):
    """A probe that can hang is worse than none: the platform's own timeout
    fires and the reason never reaches anyone."""
    monkeypatch.setattr(health, "get_sessionmaker", HangingSessionmaker())
    monkeypatch.setattr(health.get_settings(), "db_command_timeout_seconds", 0.2)

    started = asyncio.get_running_loop().time()
    response = await client.get("/readyz")
    elapsed = asyncio.get_running_loop().time() - started

    assert response.status_code == 503
    assert elapsed < 5, f"probe took {elapsed:.1f}s -- it did not honour its own deadline"
    database = next(c for c in response.json()["checks"] if c["name"] == "database")
    assert "timed out" in database["error"]


async def test_the_two_checks_run_concurrently(client, monkeypatch):
    """Sequential checks make the probe as slow as the sum of its timeouts."""
    calls: list[str] = []

    async def slow_db():
        calls.append("db-start")
        await asyncio.sleep(0.15)

    async def slow_cache():
        calls.append("cache-start")
        await asyncio.sleep(0.15)

    monkeypatch.setattr(health, "_check_database", slow_db)
    monkeypatch.setattr(health, "_check_cache", slow_cache)

    started = asyncio.get_running_loop().time()
    await client.get("/readyz")
    elapsed = asyncio.get_running_loop().time() - started

    assert calls[:2] == ["db-start", "cache-start"]
    assert elapsed < 0.28, f"{elapsed:.2f}s looks sequential, not concurrent"


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
async def test_probes_need_no_credentials(client, path):
    """A load balancer cannot send an admin key."""
    assert (await client.get(path)).status_code in (200, 503)
