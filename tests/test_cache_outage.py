"""The read path survives a Redis outage by falling back to the database.

That is what `/readyz` promises when it reports "degraded" rather than
unready for a dead cache (tests/test_health.py), and it is what keeps every
instance in the load balancer. It only holds if the read path itself does not
raise when the cache does.
"""

from __future__ import annotations

from app.cache import set_cache
from app.models import Subscription


class DeadCache:
    async def _refused(self, *a, **kw):
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    get = set = delete = incr = get_int = _refused

    async def close(self) -> None:
        pass


async def test_paying_customer_keeps_entitlements_when_redis_is_down(client, session):
    session.add(Subscription(user_id="payer", tier="pro", status="active"))
    await session.commit()
    set_cache(DeadCache())

    response = await client.get("/v1/entitlements", headers={"X-User-Id": "payer"})

    assert response.status_code == 200, response.text
    assert response.json()["tier"] == "pro"


async def test_feature_check_still_resolves_when_redis_is_down(client, session):
    # The billing and product routes resolve entitlements too; a paying
    # customer asking for a paid model must not get a 500 for our outage.
    session.add(Subscription(user_id="payer2", tier="pro", status="active"))
    await session.commit()
    set_cache(DeadCache())

    from app.services.entitlements import resolve_entitlements

    ents = await resolve_entitlements(session, "payer2")
    assert ents.tier == "pro"
