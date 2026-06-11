"""Signal routes — read-only signal history for the authenticated user."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core import db, get_current_user

router = APIRouter(tags=["Signals"])


@router.get("/core/signals")
async def get_core_signals(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.signals.find({"user_id": user_id}).sort("created_at", -1).to_list(length=200)
