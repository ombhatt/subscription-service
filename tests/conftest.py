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
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import jwt
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
        # Out of the way of every test that is not about rate limiting;
        # tests/test_rate_limit.py sets its own windows.
        "CONTACT_SALES_PER_HOUR": "1000",
        "CONTACT_SALES_PER_DAY": "1000",
        "ENTITLEMENT_CACHE_TTL": "60",
        # Explicitly blank so the suite never picks up a real key from a
        # developer's .env. Without this the flag tests pass in CI, which
        # has no key, and fail on any machine that has one -- the worst
        # direction for a test to fail in, because green CI stops meaning
        # anything. It also kept a real key out of assertion output.
        "GROWTHBOOK_CLIENT_KEY": "",
    }
)

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import Header, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import auth, stripe_client
from app.auth import CurrentUser, get_current_user, get_current_user_optional
from app.billing_provider import PROVIDER_FUNCTIONS, SubscriptionPage
from app.cache import InMemoryBackend, set_cache
from app.db import get_session
from app.main import app
from app.models import Base
from tests.metrics import counted


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_undrained: this test commits under a marking write on purpose",
    )


@pytest.fixture(autouse=True)
def no_dropped_invalidations(request):
    """Fail any test during which a plain `session.commit()` dropped the
    entitlement invalidations a write had registered.

    The detector in `app.services.entitlements` only logs, which is right in
    production and no use in a test run: a log line inside a passing test is
    read by nobody. #42 moved the marking write paths to
    `commit_and_invalidate` and missed two -- `admin.resync` and `reconcile` --
    and the suite stayed green. This turns that log line into a failure.
    """
    undrained = counted("entitlement_invalidations_total", outcome="undrained")
    yield
    if request.node.get_closest_marker("allow_undrained"):
        return
    dropped = undrained()
    assert dropped == 0, (
        f"a plain session.commit() dropped entitlement invalidations {dropped:g} "
        "time(s) in this test -- commit marking writes with commit_and_invalidate()"
    )


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


@pytest.fixture
def run_main(sessionmaker_, monkeypatch, caplog):
    """Run a job module's `main()` against the test database, as the cron would.

    Returns its exit code and the structured events it logged, keyed by event
    name: `code, events = await run_main(job)`.
    """

    async def run(job) -> tuple[int | None, dict[str, dict]]:
        monkeypatch.setattr(job, "get_sessionmaker", lambda: sessionmaker_)
        monkeypatch.setattr(job, "dispose_engine", AsyncMock())
        monkeypatch.setattr(job, "configure_logging", lambda **kw: None)
        with caplog.at_level("INFO"):
            code = await job.main()
        events = {r.context["event"]: r.context for r in caplog.records if hasattr(r, "context")}
        return code, events

    return run


async def _current_user_for_tests(x_user_id: str | None = Header(default=None)) -> CurrentUser:
    """Who is calling, for tests only.

    The real dependency verifies a Supabase JWT and has no header-trusting
    branch -- a development bypass in production code is exactly the thing that
    survives to production. Overriding it here lets every test keep saying who
    it is with a header, without that path existing in the app.
    """
    if not x_user_id:
        raise HTTPException(status_code=401, detail="missing X-User-Id (test override)")
    return CurrentUser(id=x_user_id, email=f"{x_user_id}@example.test")


async def _optional_user_for_tests(
    x_user_id: str | None = Header(default=None),
) -> CurrentUser | None:
    """Same header stub, but anonymous instead of 401 when it is absent."""
    if not x_user_id:
        return None
    return CurrentUser(id=x_user_id, email=f"{x_user_id}@example.test")


@asynccontextmanager
async def _app_client(overrides: dict):
    # The overrides live on the one shared `app`, so two clients in a test
    # would see each other's. Refuse rather than let one silently win.
    assert not app.dependency_overrides, "one app client per test"
    app.dependency_overrides.update(overrides)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def _sessions_from(sessionmaker_):
    async def override_get_session():
        async with sessionmaker_() as s:
            yield s

    return override_get_session


@pytest_asyncio.fixture
async def client(sessionmaker_):
    overrides = {
        get_session: _sessions_from(sessionmaker_),
        get_current_user: _current_user_for_tests,
        # The optional variant needs its own override, or an endpoint that uses
        # it sees every test request as anonymous while the header says otherwise.
        get_current_user_optional: _optional_user_for_tests,
    }
    async with _app_client(overrides) as c:
        yield c


@pytest_asyncio.fixture
async def real_auth_client(sessionmaker_, signer):
    """`client`, but identity comes from the app's own token verification.

    Tests that are about authentication itself need the real dependency to run;
    the header stub would only test the stub. Send `headers=bearer(signer())`
    to be someone.
    """
    async with _app_client({get_session: _sessions_from(sessionmaker_)}) as c:
        yield c


# ---------------------------------------------------------------------------
# Supabase tokens, against a keypair this suite generates
# ---------------------------------------------------------------------------

TOKEN_ISSUER = "https://project.supabase.co/auth/v1"
TOKEN_KID = "test-signing-key"
TOKEN_SUBJECT = "8f14e45f-ceea-467a-9c1e-3f2a1b6c7d80"


def token_keypair(algorithm: str):
    if algorithm == "ES256":
        private = ec.generate_private_key(ec.SECP256R1())
    else:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return private, private_pem


class _StubJWKSClient:
    """Stands in for PyJWKClient, returning the public half of our keypair."""

    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return type("Key", (), {"key": self._public_key})()


@pytest.fixture
def token_algorithm() -> str:
    """ES256, what new Supabase projects sign with. tests/test_auth_tokens.py
    overrides this to cover RS256 too."""
    return "ES256"


@pytest.fixture
def signer(token_algorithm, monkeypatch):
    """A working Supabase-shaped setup: configured URL and a known signing key."""
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")

    from app.config import get_settings

    get_settings.cache_clear()
    private, private_pem = token_keypair(token_algorithm)
    auth.set_jwks_client(_StubJWKSClient(private.public_key()))

    def mint(**overrides) -> str:
        now = datetime.now(UTC)
        claims = {
            "sub": TOKEN_SUBJECT,
            "email": "someone@example.com",
            "aud": "authenticated",
            "iss": TOKEN_ISSUER,
            "role": "authenticated",
            "iat": now,
            "exp": now + timedelta(hours=1),
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(
            claims, private_pem, algorithm=token_algorithm, headers={"kid": TOKEN_KID}
        )

    yield mint

    auth.set_jwks_client(None)
    get_settings.cache_clear()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Stripe fake
# ---------------------------------------------------------------------------


class FakeStripe:
    """Holds the state Stripe would hold, keyed by customer id.

    Tests set `stripe.subscriptions[customer] = {...}` and every code path that
    re-reads Stripe sees it -- which is exactly how the real sync behaves.

    State is kept in Stripe's own shape and handed out through the adapter's
    real parser, so every test also exercises the translation, and a fake
    payload Stripe would never send fails here rather than passing quietly.
    Its methods must match app.billing_provider.BillingProvider, which
    tests/test_billing_provider.py checks.
    """

    def __init__(self) -> None:
        self.customers: dict[str, dict] = {}
        self.subscriptions: dict[str, dict] = {}
        self.checkout_sessions: list[dict] = []
        self.portal_sessions: list[dict] = []
        self.next_customer = 0
        self.cancel_calls = 0
        # How many subscriptions list_subscriptions_page returns at a time.
        self.page_size = 100

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
        raw = self.customers.get(customer_id, {"id": customer_id, "metadata": {}})
        return stripe_client.parse_customer(raw)

    async def fetch_current_subscription(self, customer_id):
        raw = self.subscriptions.get(customer_id)
        return stripe_client.parse_subscription(raw) if raw else None

    async def create_checkout_session(
        self,
        *,
        customer_id,
        price_id,
        user_id,
        success_url,
        cancel_url,
        idempotency_key,
        trial_period_days=0,
        promo_code=None,
    ):
        session = {
            "id": f"cs_test_{len(self.checkout_sessions)}",
            "url": "https://checkout.stripe.test/session",
            "customer_id": customer_id,
            "price_id": price_id,
            "user_id": user_id,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "idempotency_key": idempotency_key,
            "trial_period_days": trial_period_days,
            "promo_code": promo_code,
        }
        self.checkout_sessions.append(session)
        return session

    async def set_cancel_at_period_end(self, subscription_id, value=True):
        self.cancel_calls += 1
        for sub in self.subscriptions.values():
            if sub["id"] == subscription_id:
                sub["cancel_at_period_end"] = bool(value)
                return
        raise AssertionError(f"no such subscription: {subscription_id}")

    async def set_subscription_metadata(self, subscription_id, metadata):
        """Stripe's semantics: keys merge, and an empty string removes one."""
        for sub in self.subscriptions.values():
            if sub["id"] == subscription_id:
                current = sub.setdefault("metadata", {})
                for key, value in metadata.items():
                    if value == "":
                        current.pop(key, None)
                    else:
                        current[key] = value
                return
        raise AssertionError(f"no such subscription: {subscription_id}")

    async def create_portal_session(self, *, customer_id, return_url, flow=None):
        session = {
            "id": "bps_test",
            "url": "https://portal.stripe.test/session",
            "customer_id": customer_id,
            "flow": flow,
        }
        self.portal_sessions.append(session)
        return session

    async def list_subscriptions_page(self, starting_after=None, limit=100):
        """Real cursor semantics, so the reconcile job's paging loop is testable.

        Set `stripe.page_size = 1` to force several pages out of a handful of
        subscriptions. Returning everything in one page (which this used to do)
        made `has_more` and the `starting_after` hand-off dead code no test
        could reach.
        """
        rows = list(self.subscriptions.values())
        size = self.page_size or limit
        start = 0
        if starting_after is not None:
            ids = [r["id"] for r in rows]
            start = ids.index(starting_after) + 1 if starting_after in ids else len(rows)
        chunk = rows[start : start + size]
        return SubscriptionPage(
            items=[stripe_client.parse_subscription(raw) for raw in chunk],
            has_more=start + size < len(rows),
        )

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
        user_id: str | None = None,
    ) -> dict:
        if user_id is not None:
            self.customers[customer_id] = {"id": customer_id, "metadata": {"user_id": user_id}}
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
                        # Real subscription items carry an id, and the portal's
                        # deep link needs it. The fake omitted it, so the code
                        # that reads it looked fine here and raised against
                        # Stripe.
                        "id": f"si_{subscription_id}",
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
    # Every function the contract names -- not a list kept here by hand, which
    # once missed a new one and let it call real Stripe with the dummy key.
    for name in PROVIDER_FUNCTIONS:
        monkeypatch.setattr(stripe_client, name, getattr(fake, name))
    return fake


def webhook_event(event_id: str, event_type: str, obj: dict) -> str:
    return json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}})


async def deliver(client, payload: str):
    return await client.post(
        "/v1/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": "t=1,v1=fake", "content-type": "application/json"},
    )
