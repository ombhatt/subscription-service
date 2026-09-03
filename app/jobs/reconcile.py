"""Nightly drift check against Stripe.

You will miss webhooks -- an outage, a bad deploy, a 500 inside a retry window.
The only question is whether you find out before the customer does. This job
walks every subscription Stripe knows about, compares it to the local row, and
re-syncs anything that disagrees.

Run nightly, and alert on a non-zero `mismatched` count:
    python -m app.jobs.reconcile
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import stripe_client
from app.db import dispose_engine, get_sessionmaker
from app.models import PAID_STATUSES, Subscription
from app.services.subscriptions import STATUS_MAP, resolve_tier, sync_subscription_from_stripe

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    checked: int = 0
    mismatched: int = 0
    repaired: int = 0
    unknown_customers: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "mismatched": self.mismatched,
            "repaired": self.repaired,
            "unknown_customers": self.unknown_customers,
            "details": self.details,
        }


async def reconcile(session: AsyncSession, *, dry_run: bool = False) -> ReconcileReport:
    report = ReconcileReport()
    starting_after: str | None = None

    while True:
        page = await stripe_client.list_subscriptions_page(starting_after=starting_after)
        rows = page.get("data", [])
        if not rows:
            break

        for remote in rows:
            report.checked += 1
            customer_id = remote.get("customer")
            if isinstance(customer_id, dict):
                customer_id = customer_id.get("id")
            if not customer_id:
                continue

            result = await session.execute(
                select(Subscription).where(Subscription.stripe_customer_id == customer_id)
            )
            local = result.scalar_one_or_none()

            if local is None:
                report.unknown_customers.append(customer_id)
                if not dry_run:
                    # The customer exists in Stripe but not against any local
                    # row; sync resolves them through customer metadata.
                    await sync_subscription_from_stripe(
                        session,
                        stripe_customer_id=customer_id,
                        reason="reconcile.unknown_customer",
                    )
                    report.repaired += 1
                continue

            expected_status = STATUS_MAP.get(remote.get("status", ""))
            price = stripe_client.subscription_price(remote)
            expected_tier, _ = resolve_tier(price)

            status_matches = expected_status is not None and local.status == expected_status.value
            tier_should_be = (
                expected_tier.value
                if (expected_status in PAID_STATUSES and expected_tier is not None)
                else "free"
            )
            tier_matches = local.tier == tier_should_be

            if status_matches and tier_matches:
                continue

            report.mismatched += 1
            report.details.append(
                {
                    "user_id": local.user_id,
                    "stripe_customer_id": customer_id,
                    "local": {"tier": local.tier, "status": local.status},
                    "stripe": {"tier": tier_should_be, "status": remote.get("status")},
                }
            )
            if not dry_run:
                await sync_subscription_from_stripe(
                    session,
                    stripe_customer_id=customer_id,
                    reason="reconcile.drift",
                )
                report.repaired += 1

        if not page.get("has_more"):
            break
        starting_after = rows[-1]["id"]

    if not dry_run:
        await session.commit()
    return report


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    async with get_sessionmaker()() as session:
        report = await reconcile(session)
    log.info("reconciliation: %s", report.as_dict())
    if report.mismatched:
        log.error("DRIFT: %d subscription(s) disagreed with Stripe", report.mismatched)
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
