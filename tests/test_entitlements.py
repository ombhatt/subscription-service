from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import get_settings
from app.models import EntitlementGrant, Subscription
from app.plans import Tier
from app.services.entitlements import (
    Entitlements,
    _ttl_for,
    invalidate_entitlements,
    resolve_entitlements,
)


async def make_sub(session, user_id: str, **kwargs) -> Subscription:
    sub = Subscription(user_id=user_id, **kwargs)
    session.add(sub)
    await session.commit()
    return sub


async def test_unknown_user_resolves_to_free(session):
    ents = await resolve_entitlements(session, "nobody")
    assert ents.tier == "free"
    assert ents.source == "default"
    assert ents.features.models == ("small",)


async def test_active_subscription_grants_its_tier(session):
    await make_sub(session, "u1", tier="pro", status="active")
    ents = await resolve_entitlements(session, "u1")
    assert ents.tier == "pro"
    assert ents.source == "subscription"
    assert "reasoning" in ents.features.models


async def test_incomplete_checkout_grants_nothing(session):
    # Payment never confirmed: the row exists, the access does not.
    await make_sub(session, "u2", tier="pro", status="incomplete")
    ents = await resolve_entitlements(session, "u2")
    assert ents.tier == "free"


def grace() -> timedelta:
    return timedelta(days=get_settings().dunning_grace_days)


async def test_past_due_keeps_access_until_the_last_hour_of_grace(session):
    past_due_since = datetime.now(UTC).replace(microsecond=0) - grace() + timedelta(hours=1)
    await make_sub(session, "u3", tier="plus", status="past_due", past_due_since=past_due_since)
    ents = await resolve_entitlements(session, "u3")
    assert ents.tier == "plus"
    assert ents.grace_ends_at == past_due_since + grace()


async def test_past_due_loses_access_an_hour_after_grace(session):
    # Beyond the window, the read path revokes even if the nightly job has not
    # run yet.
    past_due_since = datetime.now(UTC).replace(microsecond=0) - grace() - timedelta(hours=1)
    await make_sub(session, "u4", tier="plus", status="past_due", past_due_since=past_due_since)
    ents = await resolve_entitlements(session, "u4")
    assert ents.tier == "free"
    assert ents.grace_ends_at == past_due_since + grace()


async def test_grant_lifts_a_free_user(session):
    session.add(
        EntitlementGrant(user_id="u5", tier="pro", reason="press account", created_by="ops")
    )
    await session.commit()
    ents = await resolve_entitlements(session, "u5")
    assert ents.tier == "pro"
    assert ents.source == "grant"


async def test_expired_grant_is_ignored(session):
    session.add(
        EntitlementGrant(
            user_id="u6",
            tier="pro",
            reason="trial extension",
            created_by="ops",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    await session.commit()
    ents = await resolve_entitlements(session, "u6")
    assert ents.tier == "free"


async def test_grant_never_downgrades_a_paying_customer(session):
    await make_sub(session, "u7", tier="pro", status="active")
    session.add(
        EntitlementGrant(user_id="u7", tier="plus", reason="stale comp", created_by="ops")
    )
    await session.commit()
    ents = await resolve_entitlements(session, "u7")
    assert ents.tier == "pro"


async def test_resolution_is_cached_until_invalidated(session):
    sub = await make_sub(session, "u8", tier="free", status="free")
    assert (await resolve_entitlements(session, "u8")).tier == "free"

    sub.tier = "pro"
    sub.status = "active"
    await session.commit()

    # Still the cached answer...
    assert (await resolve_entitlements(session, "u8")).tier == "free"
    # ...until the write path invalidates, which is what sync does.
    await invalidate_entitlements("u8")
    assert (await resolve_entitlements(session, "u8")).tier == "pro"


# ==========================================================================
# the cache cannot outlive the grace boundary
# ==========================================================================


def _payload(seconds_left: float | None) -> Entitlements:
    """Only the grace boundary matters to the TTL, so only it is set."""
    when = None if seconds_left is None else datetime.now(UTC) + timedelta(seconds=seconds_left)
    return Entitlements.model_construct(tier=Tier.PRO, grace_ends_at=when)


def test_a_cached_entitlement_never_outlives_the_grace_window():
    """Crossing the boundary is the passage of time, not a write, so no
    invalidation can reach the cached entry. The TTL has to do it."""
    configured = get_settings().entitlement_cache_ttl
    assert _ttl_for(_payload(None)) == configured, "no window, no cap"
    assert _ttl_for(_payload(9999)) == configured, "distant window, no cap"
    for remaining, ttl in ((5.5, 5), (30.5, 30), (59.5, 59)):
        assert _ttl_for(_payload(remaining)) == ttl, (
            f"a {remaining}s window must expire before the boundary, not after it"
        )


def test_a_sub_second_window_is_not_cached_at_all():
    """A TTL of 1 outlives the boundary; a TTL of 0 means *never expire* to some
    backends. Declining to cache is the only answer wrong in neither direction."""
    assert _ttl_for(_payload(0.5)) == 0


def test_a_boundary_already_passed_needs_no_cap():
    """Past the window the answer is stable again -- free, and staying free."""
    assert _ttl_for(_payload(-100)) == get_settings().entitlement_cache_ttl
