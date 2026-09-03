"""Authentication seam.

This service does not own identity -- it attaches subscriptions to a user id
that already exists. Replace `get_current_user` with a call into your real auth
(session cookie, JWT verification, Clerk/WorkOS SDK) and nothing else here
changes.

One rule when you do: resolve the tier from *this* service, never from a claim
in the token. A downgrade has to take effect before the token expires.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException, status

from app.config import get_settings


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str | None = None


async def get_current_user(
    x_user_id: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None),
) -> CurrentUser:
    settings = get_settings()
    if settings.is_production:
        # Fail loudly rather than shipping the development stub by accident.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="get_current_user has not been wired to real authentication",
        )
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-User-Id (development authentication stub)",
        )
    return CurrentUser(id=x_user_id, email=x_user_email)


async def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not x_admin_key or x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin key required")
