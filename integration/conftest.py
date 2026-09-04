"""Test-clock integration harness.

Deliberately outside `tests/`, because `tests/conftest.py` replaces the Stripe
client with a fake and overwrites the environment with dummy keys. This suite is
the opposite: real Stripe API, real objects, real API version. It exists to
catch the class of bug the fake cannot -- where our reading of Stripe's data is
wrong rather than our logic.

Requires a sandbox key in `.env` and network access, so it never runs in CI and
is excluded from `pytest` by default (`testpaths = ["tests"]`).

    make testclock          # or: .venv/bin/pytest integration/ -v
"""

from __future__ import annotations

import os
import time

import pytest
import pytest_asyncio

# A throwaway database and an in-process cache, so this never touches the dev
# SQLite file or the running Redis. Everything else -- crucially the Stripe key
# and price ids -- still comes from .env.
os.environ["REDIS_URL"] = ""

import stripe
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models import Base
from app.stripe_client import _as_dict

# Prefixed onto every clock this suite creates. CI sets it so the cleanup step
# can delete only what CI made -- the nightly run shares a sandbox with whatever
# simulations you have open in the Dashboard, and must not touch those.
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


@pytest_asyncio.fixture
async def engine(tmp_path_factory):
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
        in which the renewal invoice is still a draft."""
        sub = _as_dict(stripe.Subscription.retrieve(subscription_id))
        from app.stripe_client import subscription_period

        _, period_end = subscription_period(sub)
        assert period_end, "subscription has no period end to advance past"
        self.advance_to(period_end + DRAFT_WINDOW_S)

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
