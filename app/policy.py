"""Pure policy helpers over a Subscription row.

Lives outside `services/` so both the write path and the read path can use it
without importing each other.
"""

from __future__ import annotations

from datetime import datetime

from app import timeutil
from app.config import get_settings
from app.models import Subscription, SubscriptionStatus
from app.timeutil import as_utc


def grace_ends_at(sub: Subscription) -> datetime | None:
    """When a past_due subscriber loses paid access. None if not past_due."""
    if sub.status != SubscriptionStatus.PAST_DUE.value or sub.past_due_since is None:
        return None
    started = as_utc(sub.past_due_since)
    return started + get_settings().dunning_grace


def grace_expired(sub: Subscription) -> bool:
    """True once the grace window has closed.

    Checked on the read path as well as by the nightly job, so a customer never
    keeps paid access just because the job has not run yet.
    """
    ends = grace_ends_at(sub)
    return ends is not None and timeutil.utcnow() >= ends
