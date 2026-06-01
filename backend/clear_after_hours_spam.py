"""Dedupe and archive repeated paper market-closed failed orders.

Default mode is dry-run. Pass --apply to archive matching order rows and keep a
single skipped_signals summary per strategy/symbol/side/reason/session date.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _today_ist_window() -> tuple[str, str, str]:
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    start_ist = ist.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_ist - timedelta(hours=5, minutes=30)
    return start_utc.isoformat(), (start_utc + timedelta(days=1)).isoformat(), ist.date().isoformat()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply archive updates. Omit for dry-run.")
    parser.add_argument("--user-id", default="", help="Optional user id scope.")
    args = parser.parse_args()

    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    now = datetime.now(timezone.utc).isoformat()
    start, end, session_date = _today_ist_window()
    user_filter = {"user_id": args.user_id} if args.user_id else {}

    query = {
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
    rows = await db.orders.find(query, {"_id": 0}).to_list(10000)
    groups = defaultdict(list)
    for row in rows:
        key = (
            row.get("user_id"),
            row.get("strategy_id") or "manual",
            str(row.get("symbol") or "").upper(),
            str(row.get("side") or "").upper(),
            "MARKET_CLOSED",
            session_date,
        )
        groups[key].append(row)

    print(f"dry_run={not args.apply}")
    print(f"matched_failed_orders={len(rows)}")
    print(f"dedupe_groups={len(groups)}")

    if args.apply:
        for (user_id, strategy_id, symbol, side, reason_code, day), items in groups.items():
            dedupe_key = "|".join([str(strategy_id), symbol, side, reason_code, day])
            await db.skipped_signals.find_one_and_update(
                {"user_id": user_id, "dedupe_key": dedupe_key},
                {
                    "$setOnInsert": {
                        "id": str(uuid.uuid4()),
                        "user_id": user_id,
                        "dedupe_key": dedupe_key,
                        "session_date": day,
                        "strategy_id": strategy_id if strategy_id != "manual" else None,
                        "symbol": symbol,
                        "side": side,
                        "status": "SKIPPED_SIGNAL",
                        "execution_status": "SKIPPED_SIGNAL",
                        "reason_code": reason_code,
                        "skip_reason": "Market closed",
                        "mode": "paper",
                        "created_at": now,
                    },
                    "$set": {
                        "last_seen_at": now,
                        "updated_at": now,
                        "status_message": f"{len(items)} signals skipped due to market closed",
                    },
                    "$inc": {"count": len(items)},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        await db.orders.update_many(
            query,
            {"$set": {
                "visibility": "hidden",
                "archived_at": now,
                "archive_reason": "deduped after-hours paper failed-order spam",
                "updated_at": now,
            }},
        )
        await db.paper_state_audit.insert_one({
            "id": str(uuid.uuid4()),
            "type": "AFTER_HOURS_SPAM_DEDUPED",
            "count": len(rows),
            "groups": len(groups),
            "created_at": now,
        })
        print("applied=true")
    else:
        print("applied=false")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
