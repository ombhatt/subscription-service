"""Test harness.

SQLite stands in for Postgres (the schema uses nothing dialect-specific beyond
a JSON column that has a SQLite variant), the cache runs in-process, and Stripe
is replaced by a small fake whose state each test sets directly. That fake is
the point: it lets the suite deliver duplicate and out-of-order webhooks, which
is what actually breaks subscription code.
"""

from __future__ import annotations

import json
import os

import pytest
import pytest_asyncio

# Must be set before app.config is imported, since Settings is cached.
os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": "sqlite+aiosqlite://",
        "REDIS_URL": "",
        "ADMIN_API_KEY": "test-admin-key",
        "STRIPE_SECRET_KEY": "sk_test_fake",
        "STRIPE_WEBHOOK_SECRET": "whsec_fake",
        "STRIPE_PRICE_PLUS_MONTHLY": "price_plus_m",
        "STRIPE_PRICE_PLUS_ANNUAL": "price_plus_y",
        "STRIPE_PRICE_PRO_MONTHLY": "price_pro_m",
        "STRIPE_PRICE_PRO_ANNUAL": "price_pro_y",
        "DUNNING_GRACE_DAYS": "7",
        "ENTITLEMENT_CACHE_TTL": "60",
    }
)

from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app import stripe_client  # noqa: E402
from app.cache import InMemoryBackend, set_cache  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_cache():
    set_cache(InMemoryBackend())
    yield
    set_cache(None)


@pytest_asyncio.fixture
async def engine(tmp_path_factory):
    # A file rather than :memory: so the request's session and the test's own
    # session can hold separate connections -- with a single shared connection
    # a read left open in the test would block the next request's write.
    path = tmp_path_factory.mktemp("db") / "test.sqlite3"
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
async def sessionmaker_(engine):
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def session(sessionmaker_):
    async with sessionmaker_() as s:
        yield s


@pytest_asyncio.fixture
async def client(sessionmaker_):
    async def override_get_session():
        async with sessionmaker_() as s:
            yield s

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Stripe fake
# ---------------------------------------------------------------------------


class FakeStripe:
    """Holds the state Stripe would hold, keyed by customer id.

    Tests set `stripe.subscriptions[customer] = {...}` and every code path that
    re-reads Stripe sees it -- which is exactly how the real sync behaves.
    """

    def __init__(self) -> None:
        self.customers: dict[str, dict] = {}
        self.subscriptions: dict[str, dict] = {}
        self.checkout_sessions: list[dict] = []
        self.portal_sessions: list[dict] = []
        self.next_customer = 0

    # -- fakes for app.stripe_client --------------------------------------

    async def ensure_customer(self, *, user_id, email, existing_id):
        if existing_id:
            return existing_id
        self.next_customer += 1
        customer_id = f"cus_test{self.next_customer}"
        self.customers[customer_id] = {
            "id": customer_id,
            "email": email,
            "metadata": {"user_id": user_id},
        }
        return customer_id

    async def retrieve_customer(self, customer_id):
        return self.customers.get(customer_id, {"id": customer_id, "metadata": {}})

    async def fetch_current_subscription(self, customer_id):
        return self.subscriptions.get(customer_id)

    async def create_checkout_session(self, **kwargs):
        session = {
            "id": f"cs_test_{len(self.checkout_sessions)}",
            "url": "https://checkout.stripe.test/session",
            **kwargs,
        }
        self.checkout_sessions.append(session)
        return session

    async def create_portal_session(self, *, customer_id, return_url):
        session = {"id": "bps_test", "url": "https://portal.stripe.test/session"}
        self.portal_sessions.append(session)
        return session

    async def list_subscriptions_page(self, starting_after=None, limit=100):
        return {"data": list(self.subscriptions.values()), "has_more": False}

    async def retrieve_charge(self, charge_id):
        return {"id": charge_id, "customer": None}

    async def retrieve_price(self, price_id):
        # The pricing page reads amounts from Stripe rather than duplicating
        # them; annual ids get the annual shape so the two are distinguishable.
        annual = price_id.endswith("_y")
        return {
            "id": price_id,
            "unit_amount": 20000 if annual else 2000,
            "currency": "usd",
            "recurring": {"interval": "year" if annual else "month"},
            "metadata": {},
        }

    def construct_event(self, payload, signature):
        # Signature verification is Stripe's code, not ours; the suite exercises
        # what we do with a verified event.
        return json.loads(payload)

    # -- helpers for tests -------------------------------------------------

    def set_subscription(
        self,
        customer_id: str,
        *,
        status: str = "active",
        price_id: str = "price_pro_m",
        price_metadata: dict | None = None,
        period_start: int = 1_700_000_000,
        period_end: int = 1_702_592_000,
        cancel_at_period_end: bool = False,
        subscription_id: str = "sub_test",
    ) -> dict:
        sub = {
            "id": subscription_id,
            "customer": customer_id,
            "status": status,
            "cancel_at_period_end": cancel_at_period_end,
            "current_period_start": period_start,
            "current_period_end": period_end,
            "items": {
                "data": [
                    {
                        "current_period_start": period_start,
                        "current_period_end": period_end,
                        "price": {
                            "id": price_id,
                            "recurring": {"interval": "month"},
                            "metadata": price_metadata or {},
                        },
                    }
                ]
            },
        }
        self.subscriptions[customer_id] = sub
        return sub


@pytest.fixture
def stripe(monkeypatch) -> FakeStripe:
    fake = FakeStripe()
    for name in (
        "ensure_customer",
        "retrieve_customer",
        "fetch_current_subscription",
        "create_checkout_session",
        "create_portal_session",
        "list_subscriptions_page",
        "retrieve_charge",
        "retrieve_price",
        "construct_event",
    ):
        monkeypatch.setattr(stripe_client, name, getattr(fake, name))
    return fake


def webhook_event(event_id: str, event_type: str, obj: dict) -> str:
    return json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}})
