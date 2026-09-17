from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status


class QuotaExceeded(Exception):
    """Raised by the enforcement path.

    Carries everything the client needs to render a useful paywall in one shot:
    what the cap was, when it lifts, and which tier removes it. A bare 429 makes
    the frontend guess.
    """

    def __init__(
        self,
        *,
        key: str,
        limit: int,
        used: int,
        reset_at: datetime,
        current_tier: str,
        upgrade_tier: str | None,
    ) -> None:
        self.key = key
        self.limit = limit
        self.used = used
        self.reset_at = reset_at
        self.current_tier = current_tier
        self.upgrade_tier = upgrade_tier
        super().__init__(f"quota '{key}' exceeded: {used}/{limit}")

    def to_payload(self) -> dict:
        return {
            "error": "quota_exceeded",
            "quota": self.key,
            "limit": self.limit,
            "used": self.used,
            "remaining": 0,
            "reset_at": self.reset_at.isoformat(),
            "current_tier": self.current_tier,
            "upgrade_tier": self.upgrade_tier,
        }


class FeatureNotEntitled(Exception):
    def __init__(self, *, feature: str, current_tier: str, required_tier: str | None) -> None:
        self.feature = feature
        self.current_tier = current_tier
        self.required_tier = required_tier
        super().__init__(f"feature '{feature}' not available on tier '{current_tier}'")

    def to_payload(self) -> dict:
        return {
            "error": "feature_not_entitled",
            "feature": self.feature,
            "current_tier": self.current_tier,
            "required_tier": self.required_tier,
        }


class RateLimited(Exception):
    """Raised by app/ratelimit.py. Carries what the caller needs to back off:
    the cap, the window it applies to, and when to try again."""

    def __init__(self, *, scope: str, limit: int, window_seconds: int, retry_after: int) -> None:
        self.scope = scope
        self.limit = limit
        self.window_seconds = window_seconds
        self.retry_after = retry_after
        super().__init__(f"rate limit for {scope!r} exceeded: {limit}/{window_seconds}s")

    def to_payload(self) -> dict:
        # `detail` because that is the field the frontend's ApiError surfaces,
        # so a blocked visitor reads this sentence rather than "Request failed".
        return {
            "error": "rate_limited",
            "detail": (
                "Too many requests from this network. "
                f"Please try again in {self.retry_after} seconds."
            ),
            "retry_after": self.retry_after,
        }


class BillingError(HTTPException):
    def __init__(self, detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> None:
        super().__init__(status_code=code, detail=detail)
