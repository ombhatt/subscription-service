"""Assembly of the entitlement payload the API returns.

Kept apart from `entitlements` (which resolves and caches the *tier*) and
`quota` (which counts) so neither has to import the other.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import EntitlementResponse, QuotaState
from app.services import quota
from app.services.entitlements import resolve_entitlements


async def entitlement_view(session: AsyncSession, user_id: str) -> EntitlementResponse:
    resolved = await resolve_entitlements(session, user_id)
    usage = await quota.states(user_id, resolved)
    return EntitlementResponse(
        user_id=resolved["user_id"],
        tier=resolved["tier"],
        display_name=resolved["display_name"],
        status=resolved["status"],
        source=resolved["source"],
        features=resolved["features"],
        quotas=[QuotaState(**state) for state in usage],
        current_period_end=resolved.get("current_period_end"),
        cancel_at_period_end=resolved.get("cancel_at_period_end", False),
        grace_ends_at=resolved.get("grace_ends_at"),
    )
