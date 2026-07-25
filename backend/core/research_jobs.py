"""Durable queue for research work that must never run inside Uvicorn."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def job_id(kind: str, user_id: str) -> str:
    return f"{kind}:{user_id}"


async def enqueue_research_job(
    db,
    *,
    kind: str,
    user_id: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    key = job_id(kind, user_id)
    current = await db.research_jobs.find_one({"_id": key}, {"status": 1})
    if current and current.get("status") in {"queued", "running"}:
        return {"job_id": key, "status": current["status"], "already_running": True}
    await db.research_jobs.update_one(
        {"_id": key},
        {
            "$set": {
                "kind": kind,
                "user_id": user_id,
                "params": params or {},
                "status": "queued",
                "queued_at": now,
                "updated_at": now,
                "error": None,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return {"job_id": key, "status": "queued", "already_running": False}


async def research_job_status(db, *, kind: str, user_id: str) -> Dict[str, Any]:
    row = await db.research_jobs.find_one({"_id": job_id(kind, user_id)}, {"_id": 0})
    return row or {"kind": kind, "user_id": user_id, "status": "idle"}
