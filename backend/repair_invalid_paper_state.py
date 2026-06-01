"""Archive invalid QuantG paper positions and bad after-hours paper order rows.

Default mode is dry-run. Pass --apply to write repair markers.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

FILLED_STATUSES = {"FILLED", "PAPER_FILLED", "COMPLETE", "COMPLETED", "TRADED"}


def _today_ist_window() -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    start_ist = ist.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_ist - timedelta(hours=5, minutes=30)
    return start_utc.isoformat(), (start_utc + timedelta(days=1)).isoformat()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply archive updates. Omit for dry-run.")
    parser.add_argument("--user-id", default="", help="Optional user id scope.")
    args = parser.parse_args()

    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    now = datetime.now(timezone.utc).isoformat()
    start, end = _today_ist_window()
    user_filter = {"user_id": args.user_id} if args.user_id else {}

    positions = await db.positions.find(
        {"mode": "paper", "visibility": {"$ne": "hidden"}, **user_filter},
        {"_id": 0},
    ).to_list(5000)
    order_ids = [p.get("source_order_id") for p in positions if p.get("source_order_id")]
    orders = await db.orders.find(
        {"id": {"$in": order_ids}},
        {"_id": 0, "id": 1, "status": 1, "mode": 1},
    ).to_list(5000)
    orders_by_id = {o["id"]: o for o in orders}

    invalid_positions = []
    for pos in positions:
        order = orders_by_id.get(pos.get("source_order_id"))
        if not pos.get("source_order_id") or not order or order.get("mode") != "paper" or str(order.get("status", "")).upper() not in FILLED_STATUSES:
            invalid_positions.append(pos)

    bad_order_query = {
        "mode": "paper",
        "visibility": {"$ne": "hidden"},
        "created_at": {"$gte": start, "$lt": end},
        "$or": [
            {"status_message": {"$regex": "market hours|market is closed|outside", "$options": "i"}},
            {"reject_reason": {"$regex": "market hours|market is closed|outside", "$options": "i"}},
            {"error_message": {"$regex": "market hours|market is closed|outside", "$options": "i"}},
        ],
        **user_filter,
    }
    bad_orders = await db.orders.find(bad_order_query, {"_id": 0}).to_list(5000)

    print(f"dry_run={not args.apply}")
    print(f"invalid_positions={len(invalid_positions)}")
    print(f"after_hours_failed_orders={len(bad_orders)}")

    if args.apply:
        for pos in invalid_positions:
            await db.paper_state_audit.insert_one({
                "id": str(uuid.uuid4()),
                "type": "INVALID_POSITION",
                "position": pos,
                "created_at": now,
            })
            await db.positions.update_one(
                {"id": pos.get("id"), "user_id": pos.get("user_id")},
                {"$set": {
                    "status": "INVALID_POSITION",
                    "invalid_reason": "source order missing or not filled",
                    "qty": 0,
                    "visibility": "hidden",
                    "archived_at": now,
                    "updated_at": now,
                }},
            )
        if bad_orders:
            await db.paper_state_audit.insert_one({
                "id": str(uuid.uuid4()),
                "type": "AFTER_HOURS_FAILED_ORDER_ARCHIVE",
                "count": len(bad_orders),
                "sample_order_ids": [row.get("id") for row in bad_orders[:20]],
                "created_at": now,
            })
            await db.orders.update_many(
                bad_order_query,
                {"$set": {
                    "visibility": "hidden",
                    "archived_at": now,
                    "archive_reason": "paper preflight market closed; expected skip, not failed order",
                    "updated_at": now,
                }},
            )
        print("applied=true")
    else:
        print("applied=false")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
