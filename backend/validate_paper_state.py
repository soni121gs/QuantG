"""Validate QuantG paper-trading invariants.

Safe by default: this script only reads MongoDB and prints a compact report.
Run from the backend folder or repo root after loading the normal .env file.
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

FILLED_STATUSES = {"FILLED", "PAPER_FILLED", "COMPLETE", "COMPLETED", "TRADED"}
BAD_SOURCE_STATUSES = {"FAILED", "REJECTED", "SKIPPED", "SKIPPED_SIGNAL", "CANCELLED", "CANCELED", "STALE", "BROKER_NOT_FOUND"}


def _today_ist_window() -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    start_ist = ist.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_ist - timedelta(hours=5, minutes=30)
    return start_utc.isoformat(), (start_utc + timedelta(days=1)).isoformat()


async def main() -> None:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    start, end = _today_ist_window()

    paper_positions = await db.positions.find(
        {"mode": "paper", "visibility": {"$ne": "hidden"}},
        {"_id": 0},
    ).to_list(5000)
    paper_order_ids = [p.get("source_order_id") for p in paper_positions if p.get("source_order_id")]
    source_orders = await db.orders.find(
        {"id": {"$in": paper_order_ids}},
        {"_id": 0, "id": 1, "status": 1, "mode": 1},
    ).to_list(5000)
    orders_by_id = {o["id"]: o for o in source_orders}

    invalid_positions = []
    for pos in paper_positions:
        source_id = pos.get("source_order_id")
        order = orders_by_id.get(source_id)
        if not source_id or not order or order.get("mode") != "paper" or str(order.get("status", "")).upper() not in FILLED_STATUSES:
            invalid_positions.append(pos)

    failed_market_hours = await db.orders.count_documents({
        "mode": "paper",
        "visibility": {"$ne": "hidden"},
        "created_at": {"$gte": start, "$lt": end},
        "$or": [
            {"status_message": {"$regex": "market hours|market is closed|outside", "$options": "i"}},
            {"reject_reason": {"$regex": "market hours|market is closed|outside", "$options": "i"}},
            {"error_message": {"$regex": "market hours|market is closed|outside", "$options": "i"}},
        ],
    })
    failed_orders_that_created_positions = await db.positions.count_documents({
        "mode": "paper",
        "source_order_status": {"$in": list(BAD_SOURCE_STATUSES)},
        "visibility": {"$ne": "hidden"},
    })
    skipped_order_rows = await db.orders.count_documents({"mode": "paper", "status": {"$in": ["SKIPPED", "SKIPPED_SIGNAL"]}})
    skipped_summaries = await db.skipped_signals.count_documents({})
    halted = await db.strategies.find(
        {"$or": [{"halted": True}, {"is_halted": True}, {"last_error": {"$regex": "contract resolution|halt", "$options": "i"}}]},
        {"_id": 0, "id": 1, "name": 1, "last_error": 1, "halt_reason": 1},
    ).to_list(100)

    print("QuantG paper state validation")
    print(f"invalid_positions_count={len(invalid_positions)}")
    print(f"positions_without_filled_source_order={len(invalid_positions)}")
    print(f"failed_orders_that_created_positions={failed_orders_that_created_positions}")
    print(f"paper_market_hours_failed_orders_today={failed_market_hours}")
    print(f"skipped_signals_counted_as_order_rows={skipped_order_rows}")
    print(f"skipped_signal_summaries={skipped_summaries}")
    print("stale_feed_status=check /api/market/feed-comparison for live user-specific feed age")
    print("market_session_mismatch=check /api/market/session-status; Dashboard/Risk/Market Hub should read this endpoint")
    print("strategy_halted_reasons=")
    for row in halted:
        print(f"- {row.get('id')} {row.get('name')}: {row.get('halt_reason') or row.get('last_error') or 'halted'}")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
