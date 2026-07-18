"""ERP Phase 0 registry cutover and approved purge.

Run from the backend container or backend directory after the pre-purge snapshot
exists. It copies the keeper strategy docs into `strategy_registry`, pauses them,
and deletes approved dead strategy config rows from `strategies`.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.strategy_registry import ERP_KEEP_STRATEGY_NAMES, ERP_PURGE_STRATEGY_NAMES


SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "data" / "archive" / "book_snapshot_2026-07"


def _registry_doc(row: Dict[str, Any], now: str) -> Dict[str, Any]:
    doc = {k: v for k, v in row.items() if k != "_id"}
    doc["registry_active"] = False
    doc["registry_phase"] = "ERP-P0"
    doc["registry_source"] = "phase0_registry_cutover"
    doc["registered_at"] = now
    return doc


async def run(apply: bool) -> Dict[str, Any]:
    if not SNAPSHOT_DIR.exists():
        raise SystemExit(f"Snapshot missing: {SNAPSHOT_DIR}")
    mongo_url = os.environ.get("MONGO_URL") or os.environ.get("MONGO_URI") or "mongodb://mongo:27017"
    db_name = os.environ.get("DB_NAME") or os.environ.get("MONGO_DB") or "quantg"
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    now = datetime.now(timezone.utc).isoformat()

    keep_rows = await db.strategies.find({"name": {"$in": sorted(ERP_KEEP_STRATEGY_NAMES)}}).to_list(200)
    purge_count = await db.strategies.count_documents({"name": {"$in": sorted(ERP_PURGE_STRATEGY_NAMES)}})
    missing_keep = sorted(ERP_KEEP_STRATEGY_NAMES - {r.get("name") for r in keep_rows})

    summary = {
        "apply": apply,
        "keepers_found": len(keep_rows),
        "keepers_missing": missing_keep,
        "purge_rows": purge_count,
    }
    if not apply:
        return summary

    for row in keep_rows:
        await db.strategy_registry.update_one(
            {"strategy_id": row.get("id")},
            {"$set": _registry_doc(row, now)},
            upsert=True,
        )
        await db.strategies.update_one(
            {"id": row.get("id")},
            {"$set": {
                "status": "paused",
                "mode": "paper",
                "manual_paused": True,
                "schedule_paused": False,
                "registry": {"active": False, "phase": "ERP-P0", "source": "strategy_registry"},
                "last_filter_reason": "Paused keeper: ERP Phase 0 registry book awaits re-judge/forward-paper.",
            }},
        )

    purge_res = await db.strategies.delete_many({"name": {"$in": sorted(ERP_PURGE_STRATEGY_NAMES)}})
    summary["registry_rows_upserted"] = len(keep_rows)
    summary["purged_rows"] = int(purge_res.deleted_count or 0)
    summary["remaining_strategy_rows"] = await db.strategies.count_documents({})
    summary["remaining_registry_rows"] = await db.strategy_registry.count_documents({})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually write registry rows and delete purge rows")
    args = parser.parse_args()
    summary = asyncio.run(run(apply=args.apply))
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
