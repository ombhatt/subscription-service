"""The single source of truth for what a tier is.

This file ships with the code and is reviewed in pull requests. Prices live in
Stripe; *limits* live here. Nothing else in the codebase may hard-code a tier
name or a numeric cap -- call `limits_for()` or read an entitlement set.

Adding a tier should mean adding one entry here plus its price ids in the
environment. If you find yourself editing a call site, the abstraction leaked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.config import get_settings

UNLIMITED = None  # a limit of None means "no cap"


class Tier(StrEnum):
    FREE = "free"
    PLUS = "plus"
    PRO = "pro"


class BillingInterval(StrEnum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


# Ordering matters: entitlement resolution takes the *highest* tier a user is
# entitled to (subscription vs. a manual grant), so this must stay ascending.
TIER_RANK: dict[Tier, int] = {Tier.FREE: 0, Tier.PLUS: 1, Tier.PRO: 2}


class QuotaWindow(StrEnum):
    """How a counter's window is chosen.

    DAILY resets at UTC midnight. BILLING_PERIOD resets on the subscriber's own
    renewal date -- which is a different day for almost every customer, and the
    reason these two are not interchangeable.
    """

    DAILY = "daily"
    BILLING_PERIOD = "billing_period"


@dataclass(frozen=True)
class Quota:
    key: str
    limit: int | None
    window: QuotaWindow


@dataclass(frozen=True)
class TierDefinition:
    tier: Tier
    display_name: str
    quotas: dict[str, Quota]
    features: dict[str, object] = field(default_factory=dict)


def _q(key: str, limit: int | None, window: QuotaWindow = QuotaWindow.DAILY) -> Quota:
    return Quota(key=key, limit=limit, window=window)


CATALOG: dict[Tier, TierDefinition] = {
    Tier.FREE: TierDefinition(
        tier=Tier.FREE,
        display_name="Free",
        quotas={
            "messages_per_day": _q("messages_per_day", 20),
            "file_uploads_per_day": _q("file_uploads_per_day", 3),
        },
        features={
            "models": ["small"],
            "context_tokens": 32_000,
            "history_retention_days": 30,
            "api_access": False,
            "support_sla": "community",
        },
    ),
    Tier.PLUS: TierDefinition(
        tier=Tier.PLUS,
        display_name="Plus",
        quotas={
            "messages_per_day": _q("messages_per_day", 300),
            "file_uploads_per_day": _q("file_uploads_per_day", 50),
        },
        features={
            "models": ["small", "large"],
            "context_tokens": 200_000,
            "history_retention_days": 365,
            "api_access": False,
            "support_sla": "48h",
        },
    ),
    Tier.PRO: TierDefinition(
        tier=Tier.PRO,
        display_name="Pro",
        quotas={
            "messages_per_day": _q("messages_per_day", 1_500),
            "file_uploads_per_day": _q("file_uploads_per_day", UNLIMITED),
        },
        features={
            "models": ["small", "large", "reasoning"],
            "context_tokens": 1_000_000,
            "history_retention_days": UNLIMITED,
            "api_access": True,
            "support_sla": "8h",
        },
    ),
}

PAID_TIERS = (Tier.PLUS, Tier.PRO)


def limits_for(tier: Tier) -> TierDefinition:
    return CATALOG[tier]


def higher_tier(a: Tier, b: Tier) -> Tier:
    return a if TIER_RANK[a] >= TIER_RANK[b] else b


def next_tier_up(tier: Tier) -> Tier | None:
    """The tier a paywall should point at. None if already at the top."""
    ranked = sorted(TIER_RANK, key=lambda t: TIER_RANK[t])
    idx = ranked.index(tier)
    return ranked[idx + 1] if idx + 1 < len(ranked) else None


def price_catalog() -> dict[str, tuple[Tier, BillingInterval]]:
    """Stripe price id -> (tier, interval), built from the environment.

    Reverse lookup only. When a subscription carries a price id that is *not*
    in here -- a grandfathered price you have since replaced -- resolution falls
    back to the `tier` metadata stamped on the Stripe price by the seed script.
    That fallback is what keeps existing subscribers on their old price working
    after you change what you charge.
    """
    s = get_settings()
    mapping = {
        s.stripe_price_plus_monthly: (Tier.PLUS, BillingInterval.MONTHLY),
        s.stripe_price_plus_annual: (Tier.PLUS, BillingInterval.ANNUAL),
        s.stripe_price_pro_monthly: (Tier.PRO, BillingInterval.MONTHLY),
        s.stripe_price_pro_annual: (Tier.PRO, BillingInterval.ANNUAL),
    }
    return {price_id: value for price_id, value in mapping.items() if price_id}


def price_id_for(tier: Tier, interval: BillingInterval) -> str | None:
    for price_id, (t, i) in price_catalog().items():
        if t is tier and i is interval:
            return price_id
    return None
