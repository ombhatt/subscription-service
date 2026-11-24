"""GET /v1/entitlements during a Postgres outage.

The read path fails open to the last known entitlements (README, "How it
works"; services/entitlements.py): a paying customer must not lose the paywall
answer because the database is unreachable while Redis still holds it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.models import Subscription

USER = {"X-User-Id": "alice"}


async def test_entitlements_are_served_from_cache_while_postgres_is_down(
    client, session, tmp_path
):
    session.add(
        Subscription(
            user_id="alice",
            tier="pro",
            status="active",
            current_period_start=datetime.now(UTC) - timedelta(days=1),
            current_period_end=datetime.now(UTC) + timedelta(days=29),
        )
    )
    await session.commit()

    warm = await client.get("/v1/entitlements", headers=USER)
    assert warm.status_code == 200, warm.text
    assert warm.json()["tier"] == "pro"

    # Postgres goes away: every connection attempt fails.
    unreachable = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/no/such/dir/db.sqlite3", poolclass=NullPool
    )
    broken = async_sessionmaker(unreachable, expire_on_commit=False, autoflush=False)

    async def down():
        async with broken() as s:
            yield s

    app.dependency_overrides[get_session] = down
    try:
        during = await client.get("/v1/entitlements", headers=USER)
    finally:
        await unreachable.dispose()

    assert during.status_code == 200, during.text
    body = during.json()
    assert body["tier"] == "pro"
    # Unknown, not zero: "we don't know" must not read as "you have none".
    assert body["credits"] is None
