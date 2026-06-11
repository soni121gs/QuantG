"""Daily reports endpoints — EOD summary per user per day.

GET /reports/daily/{date}   — one day's summary (YYYY-MM-DD)
GET /reports/daily          — last 30 days (optional ?month=YYYY-MM filter)
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from core import db, get_current_user

router = APIRouter(tags=["Reports"])


@router.get("/reports/daily")
async def list_daily_reports(month: str | None = None, user=Depends(get_current_user)):
    """Return up to 30 daily report documents for the authenticated user.

    Optional ?month=YYYY-MM returns only that month's data.
    """
    query: dict = {"user_id": user["id"]}
    if month:
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError:
            raise HTTPException(status_code=400, detail="month must be YYYY-MM")
        query["date"] = {"$regex": f"^{month}"}

    docs = await db.daily_reports.find(
        query,
        {"_id": 0},
        sort=[("date", -1)],
    ).to_list(30)
    return {"reports": docs, "count": len(docs)}


@router.get("/reports/daily/{date}")
async def get_daily_report(date: str, user=Depends(get_current_user)):
    """Return the EOD summary for a specific date (YYYY-MM-DD).

    Returns an empty skeleton if no report has been generated yet.
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    doc = await db.daily_reports.find_one(
        {"user_id": user["id"], "date": date},
        {"_id": 0},
    )
    if doc:
        return doc

    return {
        "user_id": user["id"],
        "date": date,
        "total_realized_pnl": 0.0,
        "total_unrealized_pnl": 0.0,
        "trades_taken": 0,
        "signals_fired": 0,
        "signals_filtered": 0,
        "market_regime": None,
        "best_strategy": None,
        "worst_strategy": None,
        "strategies": [],
        "generated_at": None,
    }
