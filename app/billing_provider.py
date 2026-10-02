"""What this service needs from a billing provider, in its own vocabulary.

`stripe_client` is the one implementation, and the test suite's `FakeStripe`
the other. Both return the value objects below rather than Stripe's payloads,
so the code that decides tiers and access never reaches into a provider's
nested dicts, and Stripe's status words live in one place: the adapter that
translates them.

`BillingProvider` is the contract both implementations are checked against
(tests/test_billing_provider.py). It is also the list of functions the test
fixture replaces, so a call added to the adapter cannot slip past the fake and
reach real Stripe with a dummy key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from app.models import SubscriptionStatus
from app.plans import BillingInterval


@dataclass(frozen=True)
class RemotePrice:
    id: str
    interval: BillingInterval | None
    # The `tier` metadata the seed script stamps on every price, unvalidated:
    # it is what keeps a grandfathered price resolvable once it is no longer
    # configured. Whether it names a real tier is resolve_tier's question.
    tier: str | None = None


@dataclass(frozen=True)
class RemoteSubscription:
    id: str
    customer_id: str
    # Ours, translated by the adapter; anything it does not recognise is FREE.
    status: SubscriptionStatus
    # The provider's own word, for reports and emails an operator reads.
    provider_status: str
    # Finished for good on the provider's side (cancelled, or a checkout that
    # expired unpaid), as opposed to merely not granting access right now.
    ended: bool
    price: RemotePrice | None
    # Line item ids; the portal's plan-change deep link needs the single one.
    item_ids: tuple[str, ...]
    period_start: datetime | None
    period_end: datetime | None
    cancel_at_period_end: bool
    trial_end: datetime | None
    # Flattened for storage; see stripe_client._discount for the shape.
    discount: dict | None
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def grants_access(self) -> bool:
        return self.status.grants_access


@dataclass(frozen=True)
class RemoteCustomer:
    id: str
    # The Supabase user checkout stamped on the customer; the join key for a
    # customer this service has no row for yet.
    user_id: str | None


@dataclass(frozen=True)
class SubscriptionPage:
    items: list[RemoteSubscription]
    has_more: bool


class BillingProvider(Protocol):
    async def ensure_customer(
        self, *, user_id: str, email: str | None, existing_id: str | None
    ) -> str: ...

    async def retrieve_customer(self, customer_id: str) -> RemoteCustomer: ...

    async def fetch_current_subscription(self, customer_id: str) -> RemoteSubscription | None: ...

    async def list_subscriptions_page(
        self, starting_after: str | None = None, limit: int = 100
    ) -> SubscriptionPage: ...

    async def set_cancel_at_period_end(self, subscription_id: str, value: bool = True) -> None: ...

    async def set_subscription_metadata(
        self, subscription_id: str, metadata: dict[str, str]
    ) -> None: ...

    async def create_checkout_session(
        self,
        *,
        customer_id: str,
        price_id: str,
        user_id: str,
        success_url: str,
        cancel_url: str,
        idempotency_key: str,
        trial_period_days: int = 0,
        promo_code: str | None = None,
    ) -> dict[str, Any]: ...

    async def create_portal_session(
        self, *, customer_id: str, return_url: str, flow: dict | None = None
    ) -> dict[str, Any]: ...

    async def retrieve_price(self, price_id: str) -> dict[str, Any]: ...

    async def retrieve_charge(self, charge_id: str) -> dict[str, Any]: ...

    def construct_event(self, payload: bytes, signature: str) -> dict[str, Any]: ...


# Every function the contract names, which is what the test fixture replaces.
PROVIDER_FUNCTIONS: tuple[str, ...] = tuple(
    name
    for name, value in vars(BillingProvider).items()
    if callable(value) and not name.startswith("_")
)
