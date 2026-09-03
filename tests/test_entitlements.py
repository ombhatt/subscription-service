from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import EntitlementGrant, Subscription
from app.services.entitlements import invalidate_entitlements, resolve_entitlements


async def make_sub(session, user_id: str, **kwargs) -> Subscription:
    sub = Subscription(user_id=user_id, **kwargs)
    session.add(sub)
    await session.commit()
    return sub


async def test_unknown_user_resolves_to_free(session):
    ents = await resolve_entitlements(session, "nobody")
    assert ents["tier"] == "free"
    assert ents["source"] == "default"
    assert ents["features"]["models"] == ["small"]


async def test_active_subscription_grants_its_tier(session):
    await make_sub(session, "u1", tier="pro", status="active")
    ents = await resolve_entitlements(session, "u1")
    assert ents["tier"] == "pro"
    assert ents["source"] == "subscription"
    assert "reasoning" in ents["features"]["models"]


async def test_incomplete_checkout_grants_nothing(session):
    # Payment never confirmed: the row exists, the access does not.
    await make_sub(session, "u2", tier="pro", status="incomplete")
    ents = await resolve_entitlements(session, "u2")
    assert ents["tier"] == "free"


async def test_past_due_keeps_access_inside_grace(session):
    await make_sub(
        session,
        "u3",
        tier="plus",
        status="past_due",
        past_due_since=datetime.now(UTC) - timedelta(days=2),
    )
    ents = await resolve_entitlements(session, "u3")
    assert ents["tier"] == "plus"
    assert ents["grace_ends_at"] is not None


async def test_past_due_loses_access_after_grace(session):
    # Beyond the window, the read path revokes even if the nightly job has not
    # run yet.
    await make_sub(
        session,
        "u4",
        tier="plus",
        status="past_due",
        past_due_since=datetime.now(UTC) - timedelta(days=30),
    )
    ents = await resolve_entitlements(session, "u4")
    assert ents["tier"] == "free"


async def test_grant_lifts_a_free_user(session):
    session.add(
        EntitlementGrant(user_id="u5", tier="pro", reason="press account", created_by="ops")
    )
    await session.commit()
    ents = await resolve_entitlements(session, "u5")
    assert ents["tier"] == "pro"
    assert ents["source"] == "grant"


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
    assert ents["tier"] == "free"


async def test_grant_never_downgrades_a_paying_customer(session):
    await make_sub(session, "u7", tier="pro", status="active")
    session.add(
        EntitlementGrant(user_id="u7", tier="plus", reason="stale comp", created_by="ops")
    )
    await session.commit()
    ents = await resolve_entitlements(session, "u7")
    assert ents["tier"] == "pro"


async def test_resolution_is_cached_until_invalidated(session):
    sub = await make_sub(session, "u8", tier="free", status="free")
    assert (await resolve_entitlements(session, "u8"))["tier"] == "free"

    sub.tier = "pro"
    sub.status = "active"
    await session.commit()

    # Still the cached answer...
    assert (await resolve_entitlements(session, "u8"))["tier"] == "free"
    # ...until the write path invalidates, which is what sync does.
    await invalidate_entitlements("u8")
    assert (await resolve_entitlements(session, "u8"))["tier"] == "pro"
