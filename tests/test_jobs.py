"""The two nightly jobs, together.

They were only ever tested apart, and apart they were both correct. Together
they undid each other every night: expire_grace wrote `tier = free`, which
is the Stripe mirror and not its to write, and reconcile -- correctly
comparing that mirror against Stripe -- called it drift and wrote it back.
Two audit rows per subscriber per night, and `reconciliation_drift` pinned
above zero, so the alarm for missed webhooks could never fall silent.
"""

from __future__ import annotations

from sqlalchemy import select

from app.jobs.expire_grace import expire_grace_windows
from app.jobs.reconcile import reconcile
from app.models import Subscription, SubscriptionAudit
from app.plans import Tier
from app.services.entitlements import resolve_entitlements
from tests.rows import past_due, seed


async def test_the_two_nightly_jobs_do_not_undo_each_other(session, stripe):
    """Three nights. The stored mirror holds, the served tier holds, drift stays
    at zero, and exactly one audit row is written."""
    await seed(session, **past_due(days_ago=10))
    stripe.set_subscription("cus_1", user_id="u1", status="past_due", price_id="price_pro_m")

    for night in range(3):
        await expire_grace_windows(session)
        report = await reconcile(session)
        assert report.mismatched == 0, f"night {night + 1}: the drift alert would be firing"

    row = (await session.execute(
        select(Subscription).where(Subscription.user_id == "u1"))).scalar_one()
    await session.refresh(row)
    assert row.tier == Tier.PRO.value, "the mirror is Stripe's; nothing else may write it"
    assert (await resolve_entitlements(session, "u1")).tier == Tier.FREE.value

    audits = (await session.execute(select(SubscriptionAudit).where(
        SubscriptionAudit.reason == "dunning.grace_expired"))).scalars().all()
    assert len(audits) == 1, f"one event, one row -- got {len(audits)} over three nights"
