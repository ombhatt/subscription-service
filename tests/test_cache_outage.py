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


async def test_metered_requests_are_allowed_and_counted_elsewhere_when_redis_is_down(
    client, session
):
    """Quota enforcement fails open, like the contact-sales rate limiter.

    A paying customer refused by our outage is worse than anyone getting a few
    uncounted requests: the README's read/write asymmetry. What must not happen
    is that it is silent -- the metric says enforcement is off, and the durable
    mirror still records the usage that Redis could not.
    """
    from sqlalchemy import select

    from app.models import UsageCounter
    from app.observability import quota_errors

    session.add(Subscription(user_id="payer3", tier="pro", status="active"))
    await session.commit()
    set_cache(DeadCache())
    before = quota_errors.labels(quota="messages_per_day")._value.get()

    response = await client.post(
        "/v1/chat", json={"model": "reasoning", "message": "hi"}, headers={"X-User-Id": "payer3"}
    )

    assert response.status_code == 200, response.text
    assert quota_errors.labels(quota="messages_per_day")._value.get() == before + 1
    mirrored = (
        await session.scalars(select(UsageCounter.count).where(UsageCounter.user_id == "payer3"))
    ).all()
    assert mirrored == [1], "the usage Redis could not count is still recorded"
