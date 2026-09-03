"""Support tooling.

Built on day one, not deferred: without it every billing question becomes an
engineer with a psql prompt, and that is how production databases get edited by
hand at 11pm.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin
from app.db import get_session
from app.models import EntitlementGrant, Subscription, SubscriptionAudit
from app.plans import Tier
from app.schemas import AuditEntry, GrantRequest, GrantResponse
from app.services import audit as audit_service
from app.services.entitlements import invalidate_entitlements
from app.services.subscriptions import sync_subscription_from_stripe
from app.services.view import entitlement_view

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


async def _require_subscription(session: AsyncSession, user_id: str) -> Subscription:
    result = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=404, detail=f"no subscription row for {user_id}")
    return sub


@router.get("/users/{user_id}")
async def inspect_user(user_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Everything support needs on one screen: what they are entitled to, what
    Stripe objects sit behind it, and every transition that got them here."""
    sub = await _require_subscription(session, user_id)
    view = await entitlement_view(session, user_id)

    audit_rows = await session.execute(
        select(SubscriptionAudit)
        .where(SubscriptionAudit.user_id == user_id)
        .order_by(SubscriptionAudit.created_at.desc())
        .limit(50)
    )
    grant_rows = await session.execute(
        select(EntitlementGrant)
        .where(EntitlementGrant.user_id == user_id)
        .order_by(EntitlementGrant.created_at.desc())
    )

    return {
        "entitlements": view,
        "subscription": {
            "tier": sub.tier,
            "status": sub.status,
            "stripe_customer_id": sub.stripe_customer_id,
            "stripe_subscription_id": sub.stripe_subscription_id,
            "stripe_price_id": sub.stripe_price_id,
            "current_period_end": sub.current_period_end,
            "cancel_at_period_end": sub.cancel_at_period_end,
            "past_due_since": sub.past_due_since,
            "disputed_at": sub.disputed_at,
        },
        "grants": [
            {
                "id": g.id,
                "tier": g.tier,
                "reason": g.reason,
                "expires_at": g.expires_at,
                "revoked_at": g.revoked_at,
                "created_by": g.created_by,
                "created_at": g.created_at,
            }
            for g in grant_rows.scalars().all()
        ],
        "audit": [
            AuditEntry(
                created_at=row.created_at,
                reason=row.reason,
                from_tier=row.from_tier,
                to_tier=row.to_tier,
                from_status=row.from_status,
                to_status=row.to_status,
                stripe_event_id=row.stripe_event_id,
            )
            for row in audit_rows.scalars().all()
        ],
    }


@router.post("/users/{user_id}/resync")
async def resync(user_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Force a re-read from Stripe. The fix for a webhook we missed."""
    sub = await _require_subscription(session, user_id)
    if not sub.stripe_customer_id:
        raise HTTPException(status_code=400, detail="user has no Stripe customer")
    await sync_subscription_from_stripe(
        session, stripe_customer_id=sub.stripe_customer_id, reason="admin.resync"
    )
    await session.commit()
    return {"status": "resynced", "tier": sub.tier, "subscription_status": sub.status}


@router.post("/users/{user_id}/extend-grace")
async def extend_grace(
    user_id: str,
    days: int = 7,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Push a past_due customer's cut-off back while they sort out a card."""
    sub = await _require_subscription(session, user_id)
    if sub.past_due_since is None:
        raise HTTPException(status_code=400, detail="user is not past_due")
    before = audit_service.snapshot(sub)
    sub.past_due_since = sub.past_due_since + timedelta(days=days)
    await audit_service.record(
        session,
        user_id=user_id,
        before=before,
        after=audit_service.snapshot(sub),
        reason="admin.extend_grace",
        detail={"days": days},
    )
    await session.commit()
    await invalidate_entitlements(user_id)
    return {"status": "extended", "past_due_since": sub.past_due_since}


@router.post("/grants", response_model=GrantResponse)
async def create_grant(
    body: GrantRequest,
    session: AsyncSession = Depends(get_session),
    x_admin_actor: str = Header(default="unknown", alias="X-Admin-Actor"),
) -> GrantResponse:
    """Comp a user to a tier without inventing a fake subscription.

    Resolution takes the higher of grant and subscription, so this lifts a free
    user up and never silently downgrades a paying one.
    """
    if body.tier is Tier.FREE:
        raise HTTPException(status_code=400, detail="granting free is a no-op")

    grant = EntitlementGrant(
        user_id=body.user_id,
        tier=body.tier.value,
        reason=body.reason,
        expires_at=body.expires_at,
        created_by=x_admin_actor,
    )
    session.add(grant)
    await session.flush()
    await audit_service.record(
        session,
        user_id=body.user_id,
        before=(None, None),
        after=(body.tier.value, None),
        reason="admin.grant_created",
        detail={"grant_id": grant.id, "expires_at": str(body.expires_at)},
    )
    await session.commit()
    await invalidate_entitlements(body.user_id)
    return GrantResponse(
        id=grant.id,
        user_id=grant.user_id,
        tier=Tier(grant.tier),
        reason=grant.reason,
        expires_at=grant.expires_at,
        created_by=grant.created_by,
        created_at=grant.created_at,
    )


@router.delete("/grants/{grant_id}")
async def revoke_grant(grant_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    grant = await session.get(EntitlementGrant, grant_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="grant not found")
    if grant.revoked_at is None:
        grant.revoked_at = datetime.now(UTC)
        await audit_service.record(
            session,
            user_id=grant.user_id,
            before=(grant.tier, None),
            after=(None, None),
            reason="admin.grant_revoked",
            detail={"grant_id": grant.id},
        )
        await session.commit()
        await invalidate_entitlements(grant.user_id)
    return {"status": "revoked", "grant_id": grant_id}
