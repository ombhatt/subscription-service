"""Stripe, as the suite sees it: the provider fake and webhook delivery."""

from __future__ import annotations

import json

from app import stripe_client
from app.billing_provider import SubscriptionPage


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


def webhook_event(event_id: str, event_type: str, obj: dict) -> str:
    return json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}})


async def deliver(client, payload: str):
    return await client.post(
        "/v1/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": "t=1,v1=fake", "content-type": "application/json"},
    )
