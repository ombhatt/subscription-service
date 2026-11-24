"""Assembly of the entitlement payload the API returns.

Kept apart from `entitlements` (which resolves and caches the *tier*) and
`quota` (which counts) so neither has to import the other.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import CreditBalance, EntitlementResponse, QuotaState
from app.services import credits, quota
from app.services.entitlements import resolve_entitlements

log = logging.getLogger(__name__)


async def entitlement_view(session: AsyncSession, user_id: str) -> EntitlementResponse:
    resolved = await resolve_entitlements(session, user_id)
    usage = await quota.states(user_id, resolved)
    # From Postgres, every time: the balance is not part of the cached
    # entitlements, so a grant or a spend shows on the next read.
    #
    # And the only read here that needs Postgres when the entitlements are
    # cached, so it must not take the payload down with it: the read path
    # fails open to the last known entitlements while the database is
    # unreachable. The balance is then unknown -- null, never 0, which would
    # read as "you have none". Spending is unaffected: it fails closed.
    balance: CreditBalance | None
    try:
        held = await credits.balance(session, user_id)
    except Exception:
        log.exception("credit balance read failed; serving entitlements without it")
        balance = None
    else:
        balance = CreditBalance(balance=held.credits, next_expiry_at=held.next_expiry_at)
    return EntitlementResponse(
        user_id=resolved.user_id,
        tier=resolved.tier,
        display_name=resolved.display_name,
        status=resolved.status,
        source=resolved.source,
        features=resolved.features,
        quotas=[QuotaState(**state) for state in usage],
        current_period_end=resolved.current_period_end,
        cancel_at_period_end=resolved.cancel_at_period_end,
        grace_ends_at=resolved.grace_ends_at,
        credits=balance,
    )
