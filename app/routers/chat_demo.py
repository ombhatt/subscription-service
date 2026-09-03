"""A stand-in for a real product endpoint, showing the two checks every metered
route makes: is this feature on their tier, and do they have quota left.

Delete this router once your real endpoints do the same thing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, get_current_user
from app.db import get_session
from app.errors import FeatureNotEntitled
from app.services import quota
from app.services.entitlements import (
    feature,
    minimum_tier_for_feature,
    resolve_entitlements,
)

router = APIRouter(prefix="/v1", tags=["product"])


class ChatRequest(BaseModel):
    model: str = "small"
    message: str


async def entitlements(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Injectable entitlement set. Cheap -- served from cache on almost every
    request -- so there is no reason for a handler not to ask."""
    return await resolve_entitlements(session, user.id)


@router.post("/chat")
async def chat(
    body: ChatRequest,
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    ents: dict[str, Any] = Depends(entitlements),
) -> dict:
    allowed_models = feature(ents, "models", [])
    if body.model not in allowed_models:
        required = minimum_tier_for_feature("models", body.model)
        raise FeatureNotEntitled(
            feature=f"model:{body.model}",
            current_tier=ents["tier"],
            required_tier=required.value if required else None,
        )

    # Raises QuotaExceeded, which the app turns into a 429 carrying the reset
    # time and the tier that lifts the cap.
    state = await quota.consume(
        session, user_id=user.id, key="messages_per_day", entitlements=ents
    )

    return {
        "model": body.model,
        "reply": f"[{ents['tier']}] echo: {body.message}",
        "quota": {
            "key": state["key"],
            "limit": state["limit"],
            "used": state["used"],
            "remaining": state["remaining"],
        },
    }
