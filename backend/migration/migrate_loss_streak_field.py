"""Migrate strategy_loss_streaks from the legacy `consecutive_sl_count` field
to the canonical `current_streak` field (audit Finding 1.2).

Background: live exits used to write `consecutive_sl_count` while paper exits
wrote `current_streak`; the readers were crossed, so streak throttling never
fired. The runtime now standardises on `current_streak` everywhere, which means
any pre-existing doc that only carries `consecutive_sl_count` becomes
unreadable. This one-time migration:

  1. Seeds `current_streak` from `consecutive_sl_count` ONLY where
     `current_streak` is entirely absent. (If `current_streak` already exists —
     even as 0 — the new runtime is authoritative; we do not resurrect a stale
     streak over a fresh reset.)
  2. Removes the dead `consecutive_sl_count` field from every doc.

Dry-run by default. Use --apply to write. Wallets/positions are never touched.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

from pymongo import MongoClient


@dataclass
class MigrationStats:
    docs_total: int = 0
    seeded_current_streak: int = 0
    unset_legacy_field: int = 0


def migrate(db, *, apply: bool) -> MigrationStats:
    stats = MigrationStats()
    cursor = db.strategy_loss_streaks.find(
        {"consecutive_sl_count": {"$exists": True}},
        {"current_streak": 1, "consecutive_sl_count": 1},
    )
    for doc in cursor:
        stats.docs_total += 1
        legacy = doc.get("consecutive_sl_count")
        needs_seed = "current_streak" not in doc

        update: dict = {"$unset": {"consecutive_sl_count": ""}}
        if needs_seed:
            try:
                seed = int(legacy or 0)
            except (TypeError, ValueError):
                seed = 0
            update["$set"] = {"current_streak": seed}

        if apply:
            result = db.strategy_loss_streaks.update_one({"_id": doc["_id"]}, update)
            if result.modified_count:
                stats.unset_legacy_field += 1
                if needs_seed:
                    stats.seeded_current_streak += 1
        else:
            stats.unset_legacy_field += 1
            if needs_seed:
                stats.seeded_current_streak += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate strategy_loss_streaks to canonical current_streak field"
    )
    parser.add_argument("--apply", action="store_true", help="Write updates. Without this, only report what would change.")
    parser.add_argument("--mongo-url", default=os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    parser.add_argument("--db-name", default=os.environ.get("DB_NAME", "quantg"))
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"loss-streak migration mode={mode} db={args.db_name} mongo={args.mongo_url}")

    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    db = client[args.db_name]

    stats = migrate(db, apply=args.apply)

    print(f"docs_with_legacy_field={stats.docs_total}")
    print(f"current_streak_{'seeded' if args.apply else 'would_seed'}={stats.seeded_current_streak}")
    print(f"legacy_field_{'removed' if args.apply else 'would_remove'}={stats.unset_legacy_field}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
