"""The row lock that serialises concurrent syncs for one customer.

The unit suite runs on SQLite, which silently omits `FOR UPDATE`, so the lock
test here takes real locks on the Postgres named by TEST_POSTGRES_URL. It is
marked `postgres` and skips without that URL; CI runs it in the `migrations on
postgres` job with `pytest -m postgres`. Locally, against a throwaway server:

    docker run -d --rm -p 55432:5432 -e POSTGRES_PASSWORD=postgres postgres:17-alpine
    export TEST_POSTGRES_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres
    .venv/bin/pytest -m postgres
"""

from __future__ import annotations

import asyncio
import os
import time
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import engine_kwargs
from app.models import Base, Subscription
from app.services import subscriptions
from app.services.entitlements import commit_and_invalidate

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")
LOCK_NOT_AVAILABLE = "55P03"


@pytest_asyncio.fixture
async def postgres(request):
    """Sessions on real Postgres, in a schema of their own that is dropped after."""
    if not POSTGRES_URL:
        if "postgres" in request.config.option.markexpr:
            pytest.fail("-m postgres selected, but TEST_POSTGRES_URL is not set")
        pytest.skip("needs TEST_POSTGRES_URL")
    base = create_async_engine(POSTGRES_URL, poolclass=NullPool, **engine_kwargs(POSTGRES_URL))
    schema = f"lock_test_{uuid4().hex[:12]}"
    async with base.begin() as conn:
        await conn.exec_driver_sql(f'create schema "{schema}"')
    engine = base.execution_options(schema_translate_map={None: schema})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        async with base.begin() as conn:
            await conn.exec_driver_sql(f'drop schema "{schema}" cascade')
        await base.dispose()


@pytest.mark.postgres
async def test_a_sync_waits_for_the_customers_lock_and_then_gives_up(postgres, stripe, monkeypatch):
    """While one sync holds a customer's row, a second sync for that customer
    waits, then fails with Postgres's lock timeout rather than queueing behind
    the holder's Stripe call -- and asks Stripe nothing, because the lock comes
    first. Other customers, and plain reads of the held row, are not blocked."""
    monkeypatch.setattr(get_settings(), "db_lock_timeout_seconds", 0.5)
    asked: list[str] = []

    async def recording_fetch(customer_id):
        asked.append(customer_id)
        return await stripe.fetch_current_subscription(customer_id)

    monkeypatch.setattr(subscriptions.stripe_client, "fetch_current_subscription", recording_fetch)
    stripe.set_subscription("cus_held", subscription_id="sub_held")
    stripe.set_subscription("cus_free", subscription_id="sub_free")
    async with postgres() as seed:
        seed.add_all(
            [
                Subscription(user_id="u_held", stripe_customer_id="cus_held"),
                Subscription(user_id="u_free", stripe_customer_id="cus_free"),
            ]
        )
        await seed.commit()

    async with postgres() as holder, postgres() as waiter, postgres() as other:
        assert await subscriptions._find_by_customer(holder, "cus_held", lock=True)

        assert await subscriptions._find_by_customer(other, "cus_held")
        free = await subscriptions.sync_subscription_from_stripe(
            other, stripe_customer_id="cus_free"
        )
        await commit_and_invalidate(other)
        assert (free.tier, free.status) == ("pro", "active")

        started = time.monotonic()
        with pytest.raises(DBAPIError) as refused:
            await asyncio.wait_for(
                subscriptions.sync_subscription_from_stripe(waiter, stripe_customer_id="cus_held"),
                timeout=5,
            )
        waited = time.monotonic() - started

        assert refused.value.orig.sqlstate == LOCK_NOT_AVAILABLE, refused.value
        assert 0.4 <= waited < 5, waited
        assert asked == ["cus_free"]


def test_lock_and_stripe_timeouts_are_actually_bounded():
    """Defaults must be finite. `None` here means 'wait forever', which is how
    one slow dependency becomes an outage."""
    from app.config import Settings

    s = Settings()
    assert 0 < s.db_lock_timeout_seconds < 60
    assert 0 < s.stripe_timeout_seconds < 60
    assert 0 < s.db_command_timeout_seconds < 120
    assert 0 < s.redis_timeout_seconds < 30
    # The lock wait must be shorter than the call it is waiting behind, or it
    # never fires and the bound is decorative.
    assert s.db_lock_timeout_seconds < s.stripe_timeout_seconds


def test_the_stripe_client_sets_a_timeout():
    """The SDK defaults to 80 seconds with two retries — four minutes of lock."""
    import stripe

    from app import stripe_client
    from app.config import get_settings

    stripe_client.reset_http_client()
    stripe_client._client()
    assert stripe.default_http_client._timeout == get_settings().stripe_timeout_seconds
