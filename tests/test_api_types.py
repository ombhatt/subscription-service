"""The API's types, as the web app sees them.

web/lib/openapi.json is exported from the app and web/lib/openapi.gen.ts is
generated from it; the frontend's types are aliases of that. These keep the
chain honest from this end: the committed schema is the app's, and the typed
pieces of it agree with plans.py.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import SubscriptionStatus
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
    resolved = await _resolve_from_db(session, "nobody")
    assert Entitlements.model_validate(resolved.model_dump(mode="json")) == resolved


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
