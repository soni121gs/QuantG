from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from core import db, get_current_user
from notifications import generate_user_notifications

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("")
async def list_notifications(
    unread_only: bool = False,
    severity: str | None = None,
    limit: int = 50,
    user=Depends(get_current_user),
):
    await generate_user_notifications(db, user["id"])
    query = {"user_id": user["id"]}
    if unread_only:
        query["read"] = False
    if severity:
        query["severity"] = severity
    rows = await db.notifications.find(query, {"_id": 0, "user_id": 0, "dedupe_key": 0}).sort("created_at", -1).to_list(max(1, min(limit, 100)))
    return {"notifications": rows}


@router.get("/unread-count")
async def unread_count(user=Depends(get_current_user)):
    await generate_user_notifications(db, user["id"])
    total = await db.notifications.count_documents({"user_id": user["id"], "read": False})
    critical = await db.notifications.count_documents({"user_id": user["id"], "read": False, "severity": "critical"})
    return {"unread": total, "critical": critical}


@router.post("/read-all")
async def mark_all_notifications_read(user=Depends(get_current_user)):
    res = await db.notifications.update_many(
        {"user_id": user["id"], "read": False},
        {"$set": {"read": True}},
    )
    return {"ok": True, "updated": res.modified_count}


@router.post("/{notification_id}/read")
async def mark_notification_read(notification_id: str, user=Depends(get_current_user)):
    res = await db.notifications.update_one(
        {"user_id": user["id"], "id": notification_id},
        {"$set": {"read": True}},
    )
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"ok": True}
