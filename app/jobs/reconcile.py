"""Nightly drift check against Stripe.

You will miss webhooks -- an outage, a bad deploy, a 500 inside a retry window.
The only question is whether you find out before the customer does. This job
walks every subscription Stripe knows about, compares it to the local row, and
re-syncs anything that disagrees.

Run nightly, and alert on a non-zero `mismatched` count or a non-zero exit (the
process exits 1 when any customer could not be repaired):
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
from app.observability import configure_logging, reconciliation_drift
from app.observability import event as log_event
from app.services.entitlements import commit_and_invalidate
from app.services.subscriptions import STATUS_MAP, resolve_tier, sync_subscription_from_stripe

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    checked: int = 0
    mismatched: int = 0
    repaired: int = 0
    # Customers whose check or repair raised. Each was rolled back on its own and
    # the run carried on, so they may still be on the wrong tier.
    failed: list[str] = field(default_factory=list)
    unknown_customers: list[str] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "mismatched": self.mismatched,
            "repaired": self.repaired,
            "failed": self.failed,
            "unknown_customers": self.unknown_customers,
            "details": self.details,
        }


async def reconcile(session: AsyncSession, *, dry_run: bool = False) -> ReconcileReport:
    """Walk every subscription in Stripe and repair local rows that disagree.

    One transaction per customer. `sync_subscription_from_stripe` holds a row
    lock until its transaction ends, so a single transaction for the whole run
    kept every repaired row locked across every later Stripe call -- a webhook
    for one of those customers waited on it until its lock timeout -- and one
    exception anywhere rolled back every repair made before it. Now a failure
    costs only its own customer: rolled back, logged, listed in `failed`, and
    the run moves on.
    """
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

            try:
                repaired = await _reconcile_one(
                    session, remote, customer_id, report, dry_run=dry_run
                )
                if dry_run:
                    # Nothing was written. Ending the read keeps no transaction
                    # open across the next Stripe call.
                    await session.rollback()
                else:
                    await commit_and_invalidate(session)
            except Exception:
                # The rollback also discards any invalidation marks the failed
                # sync registered: nothing was written, so nothing is stale.
                await session.rollback()
                report.failed.append(customer_id)
                log.exception("reconcile could not repair %s; continuing", customer_id)
                continue

            # Counted only once the commit has landed.
            if repaired:
                report.repaired += 1

        if not page.get("has_more"):
            break
        starting_after = rows[-1]["id"]

    return report


async def _reconcile_one(
    session: AsyncSession,
    remote: dict,
    customer_id: str,
    report: ReconcileReport,
    *,
    dry_run: bool,
) -> bool:
    """Compare one Stripe subscription with its local row. True if it re-synced."""
    result = await session.execute(
        select(Subscription).where(Subscription.stripe_customer_id == customer_id)
    )
    local = result.scalar_one_or_none()

    if local is None:
        report.unknown_customers.append(customer_id)
        if dry_run:
            return False
        # The customer exists in Stripe but not against any local
        # row; sync resolves them through customer metadata.
        await sync_subscription_from_stripe(
            session,
            stripe_customer_id=customer_id,
            reason="reconcile.unknown_customer",
        )
        return True

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
        return False

    report.mismatched += 1
    report.details.append(
        {
            "user_id": local.user_id,
            "stripe_customer_id": customer_id,
            "local": {"tier": local.tier, "status": local.status},
            "stripe": {"tier": tier_should_be, "status": remote.get("status")},
        }
    )
    if dry_run:
        return False
    await sync_subscription_from_stripe(
        session,
        stripe_customer_id=customer_id,
        reason="reconcile.drift",
    )
    return True


async def main() -> int:
    from app.config import get_settings

    configure_logging(json_logs=get_settings().log_json)
    async with get_sessionmaker()() as session:
        report = await reconcile(session)

    reconciliation_drift.set(report.mismatched)
    # The alertable signal. This runs in its own process, so a Gauge here is
    # invisible to a scrape of the web process -- alert on this log line, not on
    # the metric, unless you add a pushgateway.
    log_event(
        log,
        "reconcile.finished",
        checked=report.checked,
        mismatched=report.mismatched,
        repaired=report.repaired,
        failed=len(report.failed),
        unknown_customers=len(report.unknown_customers),
    )
    if report.mismatched:
        log.error("DRIFT: %d subscription(s) disagreed with Stripe", report.mismatched)
    if report.failed:
        log.error(
            "REPAIR FAILED: %d customer(s) could not be reconciled and may still be "
            "on the wrong tier: %s",
            len(report.failed),
            ", ".join(report.failed[:20]),
        )
    await dispose_engine()
    # Non-zero only for failures. Drift that was found and repaired is the job
    # doing its work; a customer it could not repair is the job failing.
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
