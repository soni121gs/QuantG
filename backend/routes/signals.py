"""Signal routes — read-only signal history for the authenticated user."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core import db, get_current_user

router = APIRouter(tags=["Signals"])


@router.get("/core/signals")
async def get_core_signals(user=Depends(get_current_user)):
    user_id = user["id"]
    # {"_id": 0} is load-bearing: signal docs carry a BSON ObjectId _id, which FastAPI
    # cannot serialise, so without the projection EVERY call 500s. That made the one
    # screen showing why strategies skipped unreachable — on 2026-07-24, 359 of 360
    # signals were skips the founder could not read.
    return await (
        db.signals.find({"user_id": user_id}, {"_id": 0})
        .sort("created_at", -1)
        .to_list(length=200)
    )
