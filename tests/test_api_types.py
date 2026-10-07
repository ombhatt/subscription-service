"""The API's types, as the web app sees them.

web/lib/openapi.json is exported from the app and web/lib/openapi.gen.ts is
generated from it; the frontend's types are aliases of that. These keep the
chain honest from this end: the committed schema is the app's, and the typed
pieces of it agree with plans.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.models import Subscription, SubscriptionStatus
from app.plans import CATALOG, Tier
from app.schemas import Features
from app.services.entitlements import Entitlements, _resolve_from_db
from scripts.export_openapi import TARGET, render


def test_the_committed_schema_is_the_apps():
    """A response model changed without `make api-types` fails here, rather
    than shipping a frontend typed against the old shape. (The web job checks
    the next link: that openapi.gen.ts was generated from this file.)"""
    assert TARGET.read_text() == render(), "stale web/lib/openapi.json: run `make api-types`"


@pytest.mark.parametrize("tier", list(Tier))
def test_every_tier_s_features_fit_the_typed_model(tier):
    """Features forbids unknown keys, so a feature added to a tier in plans.py
    without a field here fails -- which is what puts it in the web app's types."""
    Features(**CATALOG[tier].features)


def test_every_typed_feature_is_one_some_tier_has():
    used = {key for definition in CATALOG.values() for key in definition.features}
    assert set(Features.model_fields) == used


async def test_entitlements_survive_the_cache_round_trip(session):
    """They become JSON only at the cache. Whatever goes in must come back
    equal, dates and enums included, or a hit answers differently from a miss."""
    session.add(
        Subscription(
            user_id="dated",
            tier="pro",
            status="past_due",
            current_period_start=datetime(2026, 9, 1, tzinfo=UTC),
            current_period_end=datetime(2026, 10, 1, tzinfo=UTC),
            past_due_since=datetime(2026, 9, 30, 12, tzinfo=UTC),
        )
    )
    await session.commit()
    resolved = await _resolve_from_db(session, "dated")

    round_tripped = Entitlements.model_validate(resolved.model_dump(mode="json"))

    assert round_tripped == resolved
    assert (
        round_tripped.current_period_start,
        round_tripped.current_period_end,
        round_tripped.grace_ends_at,
    ) == (
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 9, 30, 12, tzinfo=UTC) + timedelta(days=get_settings().dunning_grace_days),
    )


def test_entitlements_cannot_be_edited_after_resolution():
    """A shared, cached answer must not be changed by one reader under another."""
    ents = Entitlements(
        user_id="u1",
        tier=Tier.PRO,
        display_name="Pro",
        status=SubscriptionStatus.ACTIVE,
        source="subscription",
        features=Features(**CATALOG[Tier.PRO].features),
        quotas=(),
    )
    with pytest.raises(ValidationError, match="frozen"):
        ents.tier = Tier.FREE  # type: ignore[misc]
