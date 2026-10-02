"""The single source of truth for what a tier is.

This file ships with the code and is reviewed in pull requests. Prices live in
Stripe; *limits* live here. Nothing else in the codebase may hard-code a tier
name or a numeric cap -- call `limits_for()` or read an entitlement set.

Adding a tier means adding it to `Tier`, `TIER_RANK` and `CATALOG` here, and,
if it is purchasable, setting STRIPE_PRICE_<TIER>_MONTHLY and
STRIPE_PRICE_<TIER>_ANNUAL (scripts/seed_stripe.py creates them and prints the
lines). If you find yourself editing a call site, the abstraction leaked.
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
    # Sales-led: no published price and no self-serve checkout. Access is
    # delivered by a manual grant against a contract negotiated out of band,
    # which is why it needs no Stripe price ids here.
    ENTERPRISE = "enterprise"


class BillingInterval(StrEnum):
    MONTHLY = "monthly"
    ANNUAL = "annual"


# Ordering matters: entitlement resolution takes the *highest* tier a user is
# entitled to (subscription vs. a manual grant), so this must stay ascending.
TIER_RANK: dict[Tier, int] = {Tier.FREE: 0, Tier.PLUS: 1, Tier.PRO: 2, Tier.ENTERPRISE: 3}


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
    # Whether this tier can be bought through self-serve checkout. Declared
    # here rather than inferred by the pricing endpoint, which used to compute
    # it as `tier is not Tier.FREE` -- a rule that silently became wrong the
    # moment a second non-purchasable tier existed.
    purchasable: bool = True
    # Sold by a conversation, not a price: the pricing page shows "Custom" and
    # a Contact sales button. Free is not purchasable either, but is not this.
    sales_led: bool = False


def _q(key: str, limit: int | None, window: QuotaWindow = QuotaWindow.DAILY) -> Quota:
    return Quota(key=key, limit=limit, window=window)


CATALOG: dict[Tier, TierDefinition] = {
    Tier.FREE: TierDefinition(
        tier=Tier.FREE,
        display_name="Free",
        purchasable=False,
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
    Tier.ENTERPRISE: TierDefinition(
        tier=Tier.ENTERPRISE,
        display_name="Enterprise",
        purchasable=False,
        sales_led=True,
        # No platform caps. Deliberate, and worth stating plainly: an
        # Enterprise agreement is negotiated in a contract, not enforced by
        # this table. If per-customer limits ever need enforcing, that is a
        # per-subscription override and a different data model -- not another
        # row here, which would make CATALOG lie about what a tier grants.
        quotas={
            "messages_per_day": _q("messages_per_day", UNLIMITED),
            "file_uploads_per_day": _q("file_uploads_per_day", UNLIMITED),
        },
        features={
            "models": ["small", "large", "reasoning"],
            "context_tokens": UNLIMITED,
            "history_retention_days": UNLIMITED,
            "api_access": True,
            "support_sla": "custom",
        },
    ),
}

# Self-serve tiers, in catalog order: the ones with Stripe prices.
PURCHASABLE_TIERS: tuple[Tier, ...] = tuple(t for t, d in CATALOG.items() if d.purchasable)


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
    configured = get_settings().stripe_price_ids
    catalog: dict[str, tuple[Tier, BillingInterval]] = {}
    for tier in PURCHASABLE_TIERS:
        for interval in BillingInterval:
            price_id = configured.get(f"{tier.value}_{interval.value}")
            if price_id:
                catalog[price_id] = (tier, interval)
    return catalog


def price_env_name(tier: Tier, interval: BillingInterval) -> str:
    """The variable a tier's price id is read from: STRIPE_PRICE_PLUS_MONTHLY."""
    return f"STRIPE_PRICE_{tier.value}_{interval.value}".upper()


def price_id_for(tier: Tier, interval: BillingInterval) -> str | None:
    for price_id, (t, i) in price_catalog().items():
        if t is tier and i is interval:
            return price_id
    return None
