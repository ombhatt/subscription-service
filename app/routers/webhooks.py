"""Stripe webhooks -- the only place paid access is ever granted.

The browser's return from checkout renders a page and nothing more: the customer
may close the tab, lose their connection, or never come back, and they have
still paid. This endpoint is what changes state.

Every handled event type ends in the same call, `sync_subscription_from_stripe`,
which re-reads the customer's real state rather than applying the event payload
as a delta. Duplicate deliveries are therefore no-ops and out-of-order
deliveries converge on the truth.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import stripe_client
from app.db import get_session
from app.models import ProcessedEvent
from app.services.subscriptions import mark_disputed, sync_subscription_from_stripe

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

# Everything that can change what a customer is entitled to. Anything not listed
# is acknowledged and ignored -- Stripe sends a great deal we do not care about,
# and 200-ing it keeps the endpoint's error rate meaningful.
SUBSCRIPTION_EVENTS = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
    "customer.subscription.pending_update_applied",
    "customer.subscription.pending_update_expired",
    "invoice.paid",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
    "invoice.marked_uncollectible",
}
DISPUTE_EVENTS = {"charge.dispute.created"}


@router.post("/stripe", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="stripe-signature"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    payload = await request.body()
    if not stripe_signature:
        raise HTTPException(status_code=400, detail="missing stripe-signature header")

    try:
        event = stripe_client.construct_event(payload, stripe_signature)
    except Exception as exc:  # signature failure, malformed body
        log.warning("rejected webhook: %s", exc)
        raise HTTPException(status_code=400, detail="signature verification failed") from exc

    event_id = event["id"]
    event_type = event["type"]

    if not await _claim(session, event_id, event_type):
        return {"status": "duplicate", "event_id": event_id}

    try:
        handled = await _handle(session, event)
        await _mark(session, event_id, "processed")
        await session.commit()
    except Exception:
        await session.rollback()
        await _mark(session, event_id, "failed", error=_short_error())
        await session.commit()
        log.exception("webhook %s (%s) failed", event_id, event_type)
        # 500 so Stripe retries on its own backoff schedule.
        raise HTTPException(status_code=500, detail="webhook handling failed") from None

    return {"status": "ok" if handled else "ignored", "event_id": event_id}


async def _claim(session: AsyncSession, event_id: str, event_type: str) -> bool:
    """Reserve this event id. False means we have already fully processed it.

    A previous *failed* attempt is allowed to run again -- that is exactly what
    Stripe's retry is for.
    """
    existing = await session.get(ProcessedEvent, event_id)
    if existing is not None:
        if existing.status == "processed":
            return False
        existing.status = "processing"
        existing.error = None
        await session.commit()
        return True

    session.add(ProcessedEvent(event_id=event_id, event_type=event_type, status="processing"))
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent delivery of the same event; the other worker owns it.
        await session.rollback()
        return False
    return True


async def _mark(session: AsyncSession, event_id: str, state: str, error: str | None = None) -> None:
    row = await session.get(ProcessedEvent, event_id)
    if row is None:
        return
    row.status = state
    row.error = error
    row.completed_at = datetime.now(UTC)


def _short_error() -> str:
    import traceback

    return traceback.format_exc()[-2000:]


async def _handle(session: AsyncSession, event: dict) -> bool:
    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type in SUBSCRIPTION_EVENTS:
        customer_id = obj.get("customer")
        if not customer_id:
            log.warning("%s carried no customer; ignoring", event_type)
            return False
        await sync_subscription_from_stripe(
            session,
            stripe_customer_id=customer_id,
            stripe_event_id=event["id"],
            reason=event_type,
        )
        return True

    if event_type in DISPUTE_EVENTS:
        customer_id = obj.get("customer")
        if not customer_id and obj.get("charge"):
            charge = await stripe_client.retrieve_charge(obj["charge"])
            customer_id = charge.get("customer")
        if not customer_id:
            log.warning("dispute %s carried no customer; ignoring", event["id"])
            return False
        await mark_disputed(
            session, stripe_customer_id=customer_id, stripe_event_id=event["id"]
        )
        await sync_subscription_from_stripe(
            session,
            stripe_customer_id=customer_id,
            stripe_event_id=event["id"],
            reason=event_type,
        )
        return True

    return False


@router.get("/recent", tags=["ops"])
async def recent_events(
    limit: int = 50, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    """Webhook health at a glance: what arrived, what failed, how far behind."""
    result = await session.execute(
        select(ProcessedEvent).order_by(ProcessedEvent.received_at.desc()).limit(limit)
    )
    return [
        {
            "event_id": row.event_id,
            "type": row.event_type,
            "status": row.status,
            "received_at": row.received_at,
            "completed_at": row.completed_at,
            "error": row.error,
        }
        for row in result.scalars().all()
    ]
