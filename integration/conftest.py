"""Test-clock integration harness.

Deliberately outside `tests/`, because `tests/conftest.py` replaces the Stripe
client with a fake and overwrites the environment with dummy keys. This suite is
the opposite: real Stripe API, real objects, real API version. It exists to
catch the class of bug the fake cannot -- where our reading of Stripe's data is
wrong rather than our logic.

Requires a sandbox key (from `.env`, or the environment in CI) and network
access, so it is excluded from `pytest` by default (`testpaths = ["tests"]`)
and runs from its own workflow, `.github/workflows/stripe-sandbox.yml`: nightly,
and on pull requests that touch the service.

    make testclock          # or: .venv/bin/pytest integration/ -v

Database: SQLite in a temporary file by default. With INTEGRATION_DATABASE_URL
set (always, in CI) it is the Supabase project's Postgres instead, so row locks,
`lock_timeout`, JSONB and the connection pooler are exercised for real rather
than approximated. Each test gets its own `ci_<epoch>_<hex>` schema, dropped at
teardown, and the credential must be the scoped `ci_runner` role from
scripts/ci_db_role.sql -- never `postgres`, because that project also holds the
service's real data.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import time
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

# A throwaway database and an in-process cache, so this never touches the dev
# SQLite file or the running Redis. Everything else -- crucially the Stripe key
# and price ids -- still comes from .env.
os.environ["REDIS_URL"] = ""

import stripe
from dotenv import dotenv_values
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import engine_kwargs
from app.models import Base
from app.stripe_client import _as_dict
from scripts.cleanup_ci_schemas import new_schema_name

# Postgres when set. Read from the environment first (CI), then .env (local runs
# that want the same database CI uses).
INTEGRATION_DATABASE_URL = os.environ.get("INTEGRATION_DATABASE_URL") or (
    dotenv_values(pathlib.Path(__file__).resolve().parents[1] / ".env").get(
        "INTEGRATION_DATABASE_URL"
    )
    or ""
)
# Credentials that can read the service's real tables. Refused outright.
ADMIN_USERS = {"postgres", "supabase_admin"}

# Prefixed onto every clock this suite creates. CI sets it so the cleanup step
# can delete only what CI made -- nightly and pull request runs share a sandbox
# with whatever simulations you have open in the Dashboard, and must not touch
# those.
CLOCK_PREFIX = os.environ.get("TEST_CLOCK_PREFIX", "")

# How long to wait for an advance to finish before giving up.
ADVANCE_TIMEOUT_S = 90
# Stripe holds a renewal invoice in `draft` for about an hour of simulated time
# before finalising and charging it, so every advance past a renewal has to
# overshoot or the invoice is still a draft when we look.
DRAFT_WINDOW_S = 2 * 60 * 60


def pytest_configure(config):
    settings = get_settings()
    if not settings.stripe_secret_key.startswith("sk_test_"):
        pytest.exit(
            "integration/ requires a Stripe *sandbox* key. Refusing to run against "
            f"a key beginning {settings.stripe_secret_key[:8]!r}.",
            returncode=1,
        )
    stripe.api_key = settings.stripe_secret_key

    if not INTEGRATION_DATABASE_URL:
        if os.environ.get("CI"):
            # A silent fallback to SQLite would pass while testing none of what
            # this run exists for -- and stop keeping the Supabase project active.
            pytest.exit(
                "INTEGRATION_DATABASE_URL is not set. CI runs integration/ against "
                "Supabase Postgres; see scripts/ci_db_role.sql.",
                returncode=1,
            )
        return
    _refuse_privileged_credentials(INTEGRATION_DATABASE_URL)


def _refuse_privileged_credentials(url: str) -> None:
    """Fail closed before any test can touch the database.

    The target project holds the service's real data. Checked by name first, so
    an admin URL never even connects, then by what the role can actually do.
    """
    user = (urlsplit(url).username or "").split(".")[0]
    if user in ADMIN_USERS:
        pytest.exit(
            f"INTEGRATION_DATABASE_URL connects as {user!r}, which can read the "
            "service's real data. Use the scoped ci_runner role "
            "(scripts/ci_db_role.sql).",
            returncode=1,
        )

    async def probe() -> tuple[bool, bool]:
        eng = create_async_engine(url, poolclass=NullPool, **engine_kwargs(url))
        try:
            async with eng.connect() as conn:
                row = (
                    await conn.exec_driver_sql(
                        "select r.rolsuper or r.rolbypassrls, "
                        "coalesce(has_table_privilege(to_regclass('public.subscriptions'), "
                        "'SELECT'), false) "
                        "from pg_roles r where r.rolname = current_user"
                    )
                ).one()
        finally:
            await eng.dispose()
        return bool(row[0]), bool(row[1])

    bypasses, reads_real_data = asyncio.run(probe())
    if bypasses or reads_real_data:
        pytest.exit(
            f"INTEGRATION_DATABASE_URL's role is over-privileged "
            f"(superuser/bypassrls={bypasses}, reads public.subscriptions={reads_real_data}). "
            "Use the scoped ci_runner role (scripts/ci_db_role.sql).",
            returncode=1,
        )


@pytest_asyncio.fixture
async def engine(tmp_path_factory):
    if not INTEGRATION_DATABASE_URL:
        path = tmp_path_factory.mktemp("clockdb") / "integration.sqlite3"
        eng = create_async_engine(
            f"sqlite+aiosqlite:///{path}",
            poolclass=NullPool,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield eng
        await eng.dispose()
        return

    # Postgres: a schema of its own per test, so tests start empty, concurrent
    # runs cannot see each other, and nothing lands in `public` where the
    # service's data lives. The translate map points every model table at it,
    # for DDL and queries alike; the service's raw SQL names no tables.
    url = INTEGRATION_DATABASE_URL
    schema = new_schema_name()
    base = create_async_engine(url, poolclass=NullPool, **engine_kwargs(url))
    async with base.begin() as conn:
        await conn.exec_driver_sql(f'create schema "{schema}"')
    eng = base.execution_options(schema_translate_map={None: schema})
    try:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield eng
    finally:
        # A killed process never gets here; scripts/cleanup_ci_schemas.py does.
        async with base.begin() as conn:
            await conn.exec_driver_sql(f'drop schema if exists "{schema}" cascade')
        await base.dispose()


@pytest_asyncio.fixture
async def session(engine):
    async with async_sessionmaker(engine, expire_on_commit=False, autoflush=False)() as s:
        yield s


class Clock:
    """One simulation, with the handful of operations these tests need.

    Stripe allows three customers per clock and three subscriptions per
    customer, so each test gets its own and deletes it afterwards -- which also
    deletes every customer and subscription created under it.
    """

    def __init__(self, name: str) -> None:
        self.started_at = int(time.time())
        raw = _as_dict(
            stripe.test_helpers.TestClock.create(
                frozen_time=self.started_at, name=f"{CLOCK_PREFIX}{name}"
            )
        )
        self.id = raw["id"]

    @property
    def now(self) -> int:
        return int(_as_dict(stripe.test_helpers.TestClock.retrieve(self.id))["frozen_time"])

    def customer(self, user_id: str, payment_method: str = "pm_card_visa") -> dict:
        """A customer on this clock, tagged so our sync can find the user.

        `metadata.user_id` is the join key the service already falls back to
        when a webhook arrives for a customer it has never seen -- so these
        tests exercise that path rather than seeding a row by hand.
        """
        return _as_dict(
            stripe.Customer.create(
                test_clock=self.id,
                email=f"{user_id}@example.test",
                payment_method=payment_method,
                invoice_settings={"default_payment_method": payment_method},
                metadata={"user_id": user_id},
            )
        )

    def subscribe(self, customer_id: str, price_id: str, **kwargs) -> dict:
        return _as_dict(
            stripe.Subscription.create(
                customer=customer_id,
                items=[{"price": price_id}],
                expand=["items.data.price"],
                **kwargs,
            )
        )

    def set_payment_method(self, customer_id: str, payment_method: str) -> None:
        """Swap the card on file -- how a working subscription starts failing.

        Attaching one of Stripe's shared test tokens mints a *new* PaymentMethod
        with its own id; setting the customer default to the token string
        instead of that id fails with "the customer does not have a payment
        method with the ID ...".
        """
        attached = _as_dict(stripe.PaymentMethod.attach(payment_method, customer=customer_id))
        stripe.Customer.modify(
            customer_id, invoice_settings={"default_payment_method": attached["id"]}
        )

    def advance_to(self, when: int) -> None:
        """Move the clock and block until Stripe says it has finished.

        Advancing is asynchronous; reading any object before the clock reports
        `ready` gives you the state from before the advance.
        """
        stripe.test_helpers.TestClock.advance(self.id, frozen_time=int(when))
        deadline = time.time() + ADVANCE_TIMEOUT_S
        while time.time() < deadline:
            status = _as_dict(stripe.test_helpers.TestClock.retrieve(self.id))["status"]
            if status == "ready":
                return
            if status not in ("advancing", "internal_failure"):
                raise AssertionError(f"unexpected test clock status: {status}")
            if status == "internal_failure":
                raise AssertionError("test clock advance failed inside Stripe")
            time.sleep(1)
        raise AssertionError(f"test clock did not become ready within {ADVANCE_TIMEOUT_S}s")

    def advance_past_renewal(self, subscription_id: str) -> None:
        """Advance just past this subscription's period end, and past the window
        in which the renewal invoice is still a draft, then wait for the outcome.

        `ready` means Stripe's time has moved, not that the renewal charge has
        been attempted: read too early and a subscription with a declining card
        is still `active`. So wait for the charge, or for a cancelling
        subscription to end.
        """
        sub = _as_dict(stripe.Subscription.retrieve(subscription_id))
        from app.stripe_client import subscription_period

        _, period_end = subscription_period(sub)
        assert period_end, "subscription has no period end to advance past"
        self.advance_to(period_end + DRAFT_WINDOW_S)

        if sub["cancel_at_period_end"]:
            self._wait_for(
                subscription_id, "the subscription to end", lambda s: s["status"] == "canceled"
            )
        else:
            before = sub["latest_invoice"]

            def charged(s: dict) -> bool:
                invoice = s["latest_invoice"]
                return bool(invoice) and invoice["id"] != before and invoice["attempted"]

            self._wait_for(subscription_id, "the renewal to be charged", charged)

    def _wait_for(self, subscription_id: str, what: str, done) -> None:
        deadline = time.time() + ADVANCE_TIMEOUT_S
        while time.time() < deadline:
            sub = _as_dict(stripe.Subscription.retrieve(subscription_id, expand=["latest_invoice"]))
            if done(sub):
                return
            time.sleep(1)
        raise AssertionError(f"timed out after {ADVANCE_TIMEOUT_S}s waiting for {what}")

    def close(self) -> None:
        try:
            stripe.test_helpers.TestClock.delete(self.id)
        except Exception:
            # A clock that failed to delete expires by itself after 30 days.
            pass


@pytest.fixture
def clock(request):
    c = Clock(name=request.node.name[:40])
    yield c
    c.close()


@pytest.fixture
def prices():
    s = get_settings()
    missing = [
        name
        for name, value in {
            "pro_monthly": s.stripe_price_pro_monthly,
            "plus_monthly": s.stripe_price_plus_monthly,
        }.items()
        if not value
    ]
    if missing:
        pytest.skip(f"prices not configured in .env: {missing}")
    return {
        "pro_monthly": s.stripe_price_pro_monthly,
        "plus_monthly": s.stripe_price_plus_monthly,
    }
