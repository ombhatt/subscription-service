"""Logs you can query, requests you can follow, and the metrics the README promises.

Counters are process-global, so every assertion here is a delta rather than an
absolute — otherwise these tests would pass or fail depending on what ran first.
"""

from __future__ import annotations

import json
import logging

from prometheus_client import REGISTRY as _DEFAULT_REGISTRY  # noqa: F401  (documents the contrast)

from app.observability import (
    JsonFormatter,
    entitlement_cache,
    quota_rejections,
    request_id_var,
    webhook_events,
)
from tests.conftest import webhook_event

ADMIN = {"X-Admin-Key": "test-admin-key"}
USER = {"X-User-Id": "alice"}


def counter_value(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


# --------------------------------------------------------------------------
# structured logging
# --------------------------------------------------------------------------


def test_logs_are_json_with_the_expected_fields():
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hello %s", args=("world",), exc_info=None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "hello world", "args must be interpolated, not left as a template"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["ts"].endswith("+00:00"), "timestamps must be unambiguous"


def test_structured_fields_ride_along():
    """`event()` puts its fields at the top level so they are queryable."""
    record = logging.LogRecord(
        name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg="webhook.handled", args=(), exc_info=None,
    )
    record.context = {"event": "webhook.handled", "duration_ms": 12.5}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "webhook.handled"
    assert payload["duration_ms"] == 12.5


def test_the_request_id_is_attached_when_one_is_set():
    token = request_id_var.set("abc123")
    try:
        record = logging.LogRecord(
            name="app.test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="something", args=(), exc_info=None,
        )
        payload = json.loads(JsonFormatter().format(record))
        assert payload["request_id"] == "abc123"
    finally:
        request_id_var.reset(token)


def test_an_exception_is_serialised_not_dropped():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            name="app.test", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info(),
        )
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


# --------------------------------------------------------------------------
# request correlation
# --------------------------------------------------------------------------


async def test_every_response_carries_a_request_id(client):
    response = await client.get("/healthz")
    assert response.headers.get("x-request-id"), "a customer must be able to quote something"
    assert len(response.headers["x-request-id"]) >= 16


async def test_an_inbound_request_id_is_honoured(client):
    """A trace started upstream should survive this hop."""
    response = await client.get("/healthz", headers={"X-Request-ID": "upstream-trace-1"})
    assert response.headers["x-request-id"] == "upstream-trace-1"


async def test_an_absurd_inbound_request_id_is_replaced(client):
    """Header values are attacker-controlled; an unbounded one would end up in
    every log line for that request."""
    response = await client.get("/healthz", headers={"X-Request-ID": "x" * 500})
    assert response.headers["x-request-id"] != "x" * 500


# --------------------------------------------------------------------------
# the metrics endpoint
# --------------------------------------------------------------------------


async def test_metrics_needs_the_admin_key(client):
    """It is a precise description of your traffic and failure rates."""
    assert (await client.get("/metrics")).status_code == 403


async def test_metrics_exposes_the_promised_series(client):
    body = (await client.get("/metrics", headers=ADMIN)).text
    for name in (
        "webhook_events_total",
        "webhook_lag_seconds",
        "payment_failures_total",
        "entitlement_cache_total",
        "reconciliation_drift",
    ):
        assert name in body, f"{name} is documented as alertable but not exposed"


# --------------------------------------------------------------------------
# the counters actually move
# --------------------------------------------------------------------------


async def test_a_handled_webhook_is_counted(client, session, stripe):
    from app.models import Subscription

    session.add(Subscription(user_id="u1", stripe_customer_id="cus_1"))
    await session.commit()
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    before = counter_value(webhook_events, event_type="customer.subscription.updated",
                           outcome="processed")

    payload = webhook_event("evt_m1", "customer.subscription.updated", {"customer": "cus_1"})
    headers = {"stripe-signature": "t=1,v1=fake", "content-type": "application/json"}
    await client.post("/v1/webhooks/stripe", content=payload, headers=headers)

    after = counter_value(webhook_events, event_type="customer.subscription.updated",
                          outcome="processed")
    assert after == before + 1


async def test_a_duplicate_is_counted_separately_from_a_grant(client, session, stripe):
    """Duplicates are normal and must not look like work being done."""
    from app.models import Subscription

    session.add(Subscription(user_id="u1", stripe_customer_id="cus_1"))
    await session.commit()
    stripe.customers["cus_1"] = {"id": "cus_1", "metadata": {"user_id": "u1"}}
    stripe.set_subscription("cus_1", status="active", price_id="price_pro_m")

    before = counter_value(webhook_events, event_type="invoice.paid", outcome="duplicate")
    payload = webhook_event("evt_m2", "invoice.paid", {"customer": "cus_1"})
    headers = {"stripe-signature": "t=1,v1=fake", "content-type": "application/json"}
    await client.post("/v1/webhooks/stripe", content=payload, headers=headers)
    await client.post("/v1/webhooks/stripe", content=payload, headers=headers)

    after = counter_value(webhook_events, event_type="invoice.paid", outcome="duplicate")
    assert after == before + 1


async def test_cache_hits_and_misses_are_distinguished(client, session):
    from app.services.entitlements import resolve_entitlements

    miss_before = counter_value(entitlement_cache, result="miss")
    hit_before = counter_value(entitlement_cache, result="hit")

    await resolve_entitlements(session, "cache-metrics-user")   # miss
    await resolve_entitlements(session, "cache-metrics-user")   # hit

    assert counter_value(entitlement_cache, result="miss") == miss_before + 1
    assert counter_value(entitlement_cache, result="hit") == hit_before + 1


async def test_a_quota_rejection_is_counted(client, session):
    from app.errors import QuotaExceeded
    from app.services import quota
    from app.services.entitlements import resolve_entitlements

    ents = await resolve_entitlements(session, "quota-metrics-user")
    limit = next(q["limit"] for q in ents["quotas"] if q["key"] == "messages_per_day")

    before = counter_value(quota_rejections, quota="messages_per_day", tier="free")
    for _ in range(limit):
        await quota.consume(session, user_id="quota-metrics-user",
                            key="messages_per_day", entitlements=ents)
    try:
        await quota.consume(session, user_id="quota-metrics-user",
                            key="messages_per_day", entitlements=ents)
    except QuotaExceeded:
        pass

    after = counter_value(quota_rejections, quota="messages_per_day", tier="free")
    assert after == before + 1
