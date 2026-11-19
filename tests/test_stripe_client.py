"""Conversion tests against real StripeObjects.

The Stripe fake in tests/fakes/stripe.py hands back plain dicts, which is right for testing
our logic but means nothing there exercises the SDK's actual return types. That
gap shipped a bug: `dict(stripe_object)` raises in stripe-python 8+, so every
read helper failed on the first real API call while the whole suite stayed
green. These tests use the SDK's own object type so that cannot recur.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import stripe
from stripe import StripeObject

from app import stripe_client
from app.config import get_settings
from app.stripe_client import _as_dict, parse_subscription


def as_stripe_object(payload: dict) -> StripeObject:
    return StripeObject.construct_from(payload, "sk_test_fake")


def test_stripe_objects_convert_all_the_way_down():
    obj = as_stripe_object(
        {
            "id": "sub_1",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_1", "metadata": {"tier": "pro"}}}]},
        }
    )
    converted = _as_dict(obj)

    assert isinstance(converted, dict)
    assert isinstance(converted["items"], dict)
    assert isinstance(converted["items"]["data"], list)
    assert isinstance(converted["items"]["data"][0], dict)
    assert isinstance(converted["items"]["data"][0]["price"], dict)
    assert converted["items"]["data"][0]["price"]["metadata"]["tier"] == "pro"


def test_plain_dicts_pass_through_unchanged():
    payload = {"id": "sub_1", "items": {"data": [{"price": {"id": "price_1"}}]}}
    assert _as_dict(payload) == payload


START = datetime.fromtimestamp(1_700_000_000, tz=UTC)
END = datetime.fromtimestamp(1_702_592_000, tz=UTC)


def test_price_and_period_read_from_a_converted_object():
    """The parts of the parser that actually walk the nested structure."""
    obj = as_stripe_object(
        {
            "id": "sub_1",
            "current_period_start": 1_700_000_000,
            "current_period_end": 1_702_592_000,
            "items": {"data": [{"price": {"id": "price_pro_m"}}]},
        }
    )
    converted = _as_dict(obj)

    remote = parse_subscription(converted)
    assert remote.price.id == "price_pro_m"
    assert (remote.period_start, remote.period_end) == (START, END)


def test_period_falls_back_to_the_subscription_item():
    """Recent Stripe API versions moved the period onto the items."""
    converted = _as_dict(
        as_stripe_object(
            {
                "id": "sub_1",
                "items": {
                    "data": [
                        {
                            "current_period_start": 1_700_000_000,
                            "current_period_end": 1_702_592_000,
                            "price": {"id": "price_pro_m"},
                        }
                    ]
                },
            }
        )
    )
    remote = parse_subscription(converted)
    assert (remote.period_start, remote.period_end) == (START, END)


# --------------------------------------------------------------------------
# how long a call can take
# --------------------------------------------------------------------------


@pytest.fixture
def unresponsive_stripe(monkeypatch):
    """The real SDK pointed at an API that accepts every request and never
    answers in time. Yields the requests it received."""
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(f"{self.command} {self.path.split('?')[0]}")
            time.sleep(1)

        do_POST = do_GET

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setattr(stripe, "api_base", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_unresponsive")
    monkeypatch.setattr(get_settings(), "stripe_timeout_seconds", 0.2)
    stripe_client.reset_http_client()
    try:
        yield received
    finally:
        stripe_client.reset_http_client()
        server.shutdown()
        server.server_close()


async def test_the_reads_sync_makes_under_its_lock_try_stripe_once(unresponsive_stripe):
    """Sync holds a row lock and a connection across these. The SDK's default
    two retries would make a 10-second timeout about 31 seconds of lock; one
    attempt keeps the timeout the ceiling it is documented to be."""
    with pytest.raises(stripe.APIConnectionError):
        await stripe_client.fetch_current_subscription("cus_slow")
    with pytest.raises(stripe.APIConnectionError):
        await stripe_client.retrieve_customer("cus_slow")

    assert unresponsive_stripe == ["GET /v1/subscriptions", "GET /v1/customers/cus_slow"]


async def test_writes_keep_the_sdks_retries(unresponsive_stripe):
    """Not made under the lock, and worth retrying: the SDK puts an
    idempotency key on each one it retries."""
    with pytest.raises(stripe.APIConnectionError):
        await stripe_client.set_cancel_at_period_end("sub_slow")

    assert unresponsive_stripe == ["POST /v1/subscriptions/sub_slow"] * 3
