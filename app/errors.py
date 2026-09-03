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


class BillingError(HTTPException):
    def __init__(self, detail: str, code: int = status.HTTP_400_BAD_REQUEST) -> None:
        super().__init__(status_code=code, detail=detail)
