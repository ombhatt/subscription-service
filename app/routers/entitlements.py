from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, get_current_user
from app.db import get_session
from app.schemas import EntitlementResponse
from app.services.view import entitlement_view

router = APIRouter(prefix="/v1/entitlements", tags=["entitlements"])


@router.get("", response_model=EntitlementResponse)
async def my_entitlements(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> EntitlementResponse:
    """What this user may do right now.

    The one call the product makes. Clients should branch on quotas and features
    here, never on the tier name -- that way changing a limit is a config edit,
    not a frontend release.
    """
    return await entitlement_view(session, user.id)
