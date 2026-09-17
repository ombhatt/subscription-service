"""The public write path's abuse control.

`POST /v1/billing/contact-sales` takes no credential and inserts a row, so the
only thing between a script and an unbounded table is this limiter. These tests
move the clock rather than sleeping, so the windows are exercised exactly.
"""

from __future__ import annotations

import pytest

from app.cache import set_cache
from app.config import get_settings
from app.observability import rate_limit_errors, rate_limit_rejections
from app.ratelimit import Window

LEAD = {"email": "cto@acme.com"}


def rejections(window: str) -> float:
    return rate_limit_rejections.labels(scope="contact_sales", window=window)._value.get()


def errors() -> float:
    return rate_limit_errors.labels(scope="contact_sales")._value.get()


@pytest.fixture
def clock(monkeypatch):
    """A movable clock for app.ratelimit."""
    from app import ratelimit

    now = [1_800_000_000.0]
    monkeypatch.setattr(ratelimit, "_now", lambda: now[0])
    return now


@pytest.fixture
def windows(monkeypatch):
    """Small windows, so the tests say 3-per-minute rather than 5-per-hour."""
    from app import ratelimit

    def small():
        return (Window("minute", 3, 60), Window("day", 5, 86_400))

    monkeypatch.setattr(ratelimit, "contact_sales_windows", small)


@pytest.fixture
def trust_proxy(monkeypatch):
    monkeypatch.setattr(get_settings(), "trust_proxy_headers", True)


async def post(client, ip: str | None = None, body: dict | None = None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return await client.post("/v1/billing/contact-sales", json=body or LEAD, headers=headers)


async def test_requests_under_the_limit_are_accepted(client, windows, clock):
    for _ in range(3):
        assert (await post(client)).status_code == 201


async def test_the_request_over_the_limit_is_refused_with_retry_after(client, windows, clock):
    before = rejections("minute")
    for _ in range(3):
        await post(client)

    blocked = await post(client)

    assert blocked.status_code == 429
    body = blocked.json()
    assert body["error"] == "rate_limited"
    assert body["retry_after"] > 0
    assert blocked.headers["Retry-After"] == str(body["retry_after"])
    # The frontend shows `detail`; a blocked visitor should read a sentence.
    assert "try again" in body["detail"].lower()
    assert rejections("minute") == before + 1


async def test_nothing_is_written_for_a_refused_request(client, session, windows, clock):
    from sqlalchemy import func, select

    from app.models import SalesInquiry

    for _ in range(4):
        await post(client)

    written = (await session.execute(select(func.count()).select_from(SalesInquiry))).scalar_one()
    assert written == 3, "the refused request must not reach the table"


async def test_the_window_reopens_once_it_has_passed(client, windows, clock):
    for _ in range(3):
        await post(client)
    assert (await post(client)).status_code == 429

    clock[0] += 60  # into the next minute

    assert (await post(client)).status_code == 201


async def test_a_drip_under_the_short_limit_still_hits_the_daily_cap(client, windows, clock):
    """The patient version of the same script: never bursty, still a flood."""
    accepted = 0
    for _ in range(6):
        if (await post(client)).status_code == 201:
            accepted += 1
        clock[0] += 60  # a fresh minute window every time

    assert accepted == 5, "the daily window is what stops this"
    assert (await post(client)).status_code == 429


async def test_callers_are_counted_separately(client, windows, clock, trust_proxy):
    for _ in range(3):
        assert (await post(client, ip="203.0.113.10")).status_code == 201
    assert (await post(client, ip="203.0.113.10")).status_code == 429

    assert (await post(client, ip="198.51.100.7")).status_code == 201, (
        "one abusive caller must not block everyone else"
    )


async def test_a_forged_header_is_ignored_unless_a_proxy_is_trusted(client, windows, clock):
    """Without TRUST_PROXY_HEADERS the header is attacker-controlled noise: a
    new address per request would be a free pass."""
    codes = [(await post(client, ip=f"203.0.113.{n}")).status_code for n in range(5)]
    assert codes == [201, 201, 201, 429, 429]


async def test_malformed_requests_are_counted_too(client, windows, clock):
    """The limiter is a dependency, so it runs before the body is validated --
    otherwise posting junk would be unlimited."""
    for _ in range(3):
        assert (await post(client, body={"email": "not-an-email"})).status_code == 422

    assert (await post(client)).status_code == 429


async def test_a_cache_failure_allows_the_request_and_is_counted(client, windows, clock):
    """Fails open: a Redis blip must not cost a sales lead. The counter is then
    the only sign the endpoint is unprotected."""

    class Broken:
        async def incr(self, key, ttl):
            raise ConnectionError("redis is gone")

    set_cache(Broken())
    before = errors()

    for _ in range(5):
        assert (await post(client)).status_code == 201

    assert errors() == before + 5


async def test_a_window_set_to_zero_is_disabled(client, clock, monkeypatch):
    from app import ratelimit

    monkeypatch.setattr(ratelimit, "contact_sales_windows", lambda: (Window("minute", 0, 60),))
    for _ in range(10):
        assert (await post(client)).status_code == 201


def test_the_shipped_windows_come_from_settings():
    """The tests above inject their own windows, so nothing else would notice a
    window being dropped from the real configuration."""
    from app.ratelimit import contact_sales_windows

    settings = get_settings()
    shipped = {w.name: w for w in contact_sales_windows()}

    assert shipped["hour"].limit == settings.contact_sales_per_hour
    assert shipped["hour"].seconds == 3600
    assert shipped["day"].limit == settings.contact_sales_per_day
    assert shipped["day"].seconds == 86_400
