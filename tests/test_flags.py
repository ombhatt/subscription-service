"""Feature flags, and specifically the ways they must not break the app.

The GrowthBook SDK is fail-*closed*. Verified against the real package before
any of this was written: when `initialize()` cannot reach the API it returns
False, and every later evaluation raises

    RuntimeError: GrowthBook client not properly initialized

Called straight from a request handler that is a 500 on every path that reads a
flag -- the flag service taking down the application it exists to protect. So
the wrapper, not the SDK, is what these tests are about.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app import flags
from app.models import Subscription
from tests.metrics import counted

APP = pathlib.Path(flags.__file__).resolve().parent
ADMIN = {"X-Admin-Key": "test-admin-key"}
USER = {"X-User-Id": "alice"}


class ExplodingClient:
    """A client that raises the way an uninitialised real one does."""

    async def get_feature_value(self, *args, **kwargs):
        raise RuntimeError("GrowthBook client not properly initialized")

    async def close(self):
        return None


class StubClient:
    """Answers from a dict, the way a healthy client answers from its snapshot."""

    def __init__(self, values: dict):
        self.values = values
        self.calls: list[tuple[str, str]] = []

    async def get_feature_value(self, name, default, user_context):
        self.calls.append((name, user_context.attributes.get("id")))
        return self.values.get(name, default)

    async def close(self):
        return None


# --------------------------------------------------------------------------
# the failure modes
# --------------------------------------------------------------------------


async def test_an_unreachable_flag_service_returns_the_compiled_in_default():
    """The reason this module exists."""
    flags.set_client_for_tests(None)
    assert await flags.is_enabled("checkout-enabled") is True


async def test_a_raising_client_returns_the_default_rather_than_propagating():
    """An uninitialised SDK raises on every call. That must stop here."""
    flags.set_client_for_tests(ExplodingClient())
    try:
        assert await flags.is_enabled("checkout-enabled") is True
    finally:
        flags.set_client_for_tests(None)


async def test_a_failure_is_counted_separately_from_an_answer():
    """A rising error rate is how you find out flags are silently not applying."""
    errors = counted("feature_flag_evaluations_total", flag="checkout-enabled", source="error")
    flags.set_client_for_tests(ExplodingClient())
    try:
        await flags.is_enabled("checkout-enabled")
    finally:
        flags.set_client_for_tests(None)
    assert errors() == 1


async def test_an_unknown_flag_is_loud_rather_than_quietly_false():
    """Falling back to False for a flag meant to default on is how a kill
    switch kills the wrong thing."""
    flags.set_client_for_tests(None)
    unknown = counted("feature_flag_evaluations_total", flag="not-a-real-flag", source="unknown")
    assert await flags.value("not-a-real-flag") is None
    assert unknown() == 1


async def test_init_never_raises_even_when_everything_is_wrong(monkeypatch):
    """Startup must survive a flag service that is down."""
    monkeypatch.setattr(flags.get_settings(), "growthbook_client_key", "sdk-nope")
    monkeypatch.setattr(flags.get_settings(), "growthbook_api_host", "http://127.0.0.1:9")
    monkeypatch.setattr(flags.get_settings(), "growthbook_timeout_seconds", 1.0)
    assert await flags.init_flags() is False
    # And the service is usable afterwards.
    assert await flags.is_enabled("checkout-enabled") is True


async def test_no_client_key_is_a_normal_state_not_a_failure(monkeypatch):
    """The ordinary local and CI path.

    Sets the key explicitly rather than reading whatever the environment
    happens to hold: the first version asserted on ambient config, so it passed
    in CI and failed on any machine with a real key in .env -- and printed that
    key into the failure output on the way.
    """
    monkeypatch.setattr(flags.get_settings(), "growthbook_client_key", "")
    assert await flags.init_flags() is False
    assert await flags.is_enabled("checkout-enabled") is True


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


async def test_a_configured_flag_overrides_the_default():
    remote = counted("feature_flag_evaluations_total", flag="checkout-enabled", source="remote")
    flags.set_client_for_tests(StubClient({"checkout-enabled": False}))
    try:
        assert await flags.is_enabled("checkout-enabled") is False
        assert remote() == 1
    finally:
        flags.set_client_for_tests(None)


async def test_the_user_id_is_passed_as_the_bucketing_key():
    """Percentage rollouts are meaningless if every request hashes differently."""
    stub = StubClient({})
    flags.set_client_for_tests(stub)
    try:
        await flags.is_enabled("checkout-enabled", user_id="alice")
        assert stub.calls == [("checkout-enabled", "alice")]
    finally:
        flags.set_client_for_tests(None)


async def test_an_anonymous_read_still_has_a_stable_key():
    stub = StubClient({})
    flags.set_client_for_tests(stub)
    try:
        await flags.is_enabled("checkout-enabled")
        assert stub.calls[0][1] == "anonymous"
    finally:
        flags.set_client_for_tests(None)


def _flags_read_in_app() -> set[str]:
    """Every string-literal flag name passed to `is_enabled` or `value` from
    `app.flags`, found by walking the AST of each module under app/.

    A flag name built at runtime is invisible to this.
    """
    readers = {"is_enabled", "value"}
    found: set[str] = set()
    for path in APP.rglob("*.py"):
        if path.name == "flags.py":
            continue
        tree = ast.parse(path.read_text())
        imported = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "app.flags"
            for alias in node.names
            if alias.name in readers
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            direct = isinstance(func, ast.Name) and func.id in imported
            via_module = (
                isinstance(func, ast.Attribute)
                and func.attr in readers
                and isinstance(func.value, ast.Name)
                and func.value.id == "flags"
            )
            first = node.args[0]
            if (direct or via_module) and isinstance(first, ast.Constant):
                found.add(first.value)
    return found


async def test_every_flag_read_in_the_app_has_a_default():
    """A flag read but not declared returns None, which is never what the
    caller meant. This is the guard for that."""
    read = _flags_read_in_app()
    assert "checkout-enabled" in read, f"flag discovery is broken, it found only {read}"
    undeclared = read - set(flags.DEFAULTS)
    assert not undeclared, f"read in app/ but missing from DEFAULTS: {sorted(undeclared)}"
    for name, default in flags.DEFAULTS.items():
        assert default is not None, f"{name} has no usable default"


# --------------------------------------------------------------------------
# the kill switch, end to end
# --------------------------------------------------------------------------


async def test_checkout_works_when_the_flag_is_on(client, stripe, session):
    flags.set_client_for_tests(None)  # default: enabled
    response = await client.post(
        "/v1/billing/checkout",
        headers=USER,
        json={"tier": "pro", "interval": "monthly"},
    )
    assert response.status_code == 200
    assert response.json()["checkout_url"]


async def test_the_kill_switch_refuses_checkout_cleanly(client, stripe, session):
    """503 with an explanation beats failing at a random point inside Stripe."""
    flags.set_client_for_tests(StubClient({"checkout-enabled": False}))
    try:
        response = await client.post(
            "/v1/billing/checkout",
            headers=USER,
            json={"tier": "pro", "interval": "monthly"},
        )
    finally:
        flags.set_client_for_tests(None)

    assert response.status_code == 503
    assert "temporarily unavailable" in response.json()["detail"]
    assert stripe.checkout_sessions == [], "Stripe must not be called at all"


async def test_the_kill_switch_does_not_touch_existing_access(client, stripe, session):
    """Turning off new purchases must not revoke anything already paid for."""
    session.add(Subscription(user_id="alice", tier="pro", status="active"))
    await session.commit()

    flags.set_client_for_tests(StubClient({"checkout-enabled": False}))
    try:
        entitlements = await client.get("/v1/entitlements", headers=USER)
    finally:
        flags.set_client_for_tests(None)
    assert entitlements.status_code == 200
    assert entitlements.json()["tier"] == "pro"
    assert entitlements.json()["source"] == "subscription"


@pytest.fixture(autouse=True)
def _no_leaked_client():
    """A stub left installed would silently change every later test."""
    yield
    flags.set_client_for_tests(None)


# --------------------------------------------------------------------------
# lifecycle, which is where the fail-safe is actually decided
# --------------------------------------------------------------------------


async def test_a_successful_connection_installs_the_client(monkeypatch):
    created: dict = {}

    class OkClient:
        def __init__(self, options):
            created["options"] = options

        async def initialize(self):
            return True

        async def close(self):
            created["closed"] = True

    monkeypatch.setattr(flags.get_settings(), "growthbook_client_key", "sdk-real")
    monkeypatch.setattr(flags.get_settings(), "growthbook_timeout_seconds", 3.0)
    monkeypatch.setattr(flags, "GrowthBookClient", OkClient)
    monkeypatch.setattr(flags, "Options", lambda **kw: kw)

    assert await flags.init_flags() is True
    # The outbound call is bounded, like every other one in this service.
    assert created["options"]["http_connect_timeout"] == 3
    assert created["options"]["http_read_timeout"] == 3

    await flags.close_flags()
    assert created.get("closed") is True


async def test_a_client_that_cannot_be_constructed_is_survivable(monkeypatch):
    def explode(options):
        raise ValueError("bad client key")

    monkeypatch.setattr(flags.get_settings(), "growthbook_client_key", "sdk-bad")
    monkeypatch.setattr(flags, "GrowthBookClient", explode)
    monkeypatch.setattr(flags, "Options", lambda **kw: kw)

    assert await flags.init_flags() is False
    assert await flags.is_enabled("checkout-enabled") is True


async def test_initialize_returning_false_drops_the_client(monkeypatch):
    """Keeping an uninitialised client would cost an exception per evaluation
    to arrive at the same default."""

    class RefusingClient:
        def __init__(self, options):
            pass

        async def initialize(self):
            return False

        async def close(self):
            return None

    monkeypatch.setattr(flags.get_settings(), "growthbook_client_key", "sdk-refuses")
    monkeypatch.setattr(flags, "GrowthBookClient", RefusingClient)
    monkeypatch.setattr(flags, "Options", lambda **kw: kw)

    assert await flags.init_flags() is False
    assert flags._client is None


async def test_the_sdk_being_absent_is_survivable(monkeypatch):
    """It is pinned in the lock, but a wrapper whose whole job is not failing
    should not fail on an import either."""
    monkeypatch.setattr(flags.get_settings(), "growthbook_client_key", "sdk-real")
    monkeypatch.setattr(flags, "GrowthBookClient", None)
    assert await flags.init_flags() is False


async def test_closing_survives_a_client_that_refuses_to_close():
    class BadCloser:
        async def close(self):
            raise RuntimeError("nope")

    flags.set_client_for_tests(BadCloser())
    await flags.close_flags()          # must not raise
    assert flags._client is None


async def test_closing_when_nothing_was_opened_is_a_no_op():
    flags.set_client_for_tests(None)
    await flags.close_flags()
    assert flags._client is None
