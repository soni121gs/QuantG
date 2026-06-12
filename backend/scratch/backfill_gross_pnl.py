"""Backfill exit-order gross_pnl from completed trade records.

Dry-run by default. Use --apply to write only orders whose gross_pnl is missing
or still zero. Wallet balances and fills are never modified.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Any, Optional

from pymongo import MongoClient


ZERO_GROSS_FILTER = {
    "$or": [
        {"gross_pnl": {"$exists": False}},
        {"gross_pnl": None},
        {"gross_pnl": 0},
        {"gross_pnl": 0.0},
    ]
}


@dataclass
class BackfillStats:
    trade_candidates: int = 0
    trade_matched_orders: int = 0
    trade_updates: int = 0
    fallback_candidates: int = 0
    fallback_updates: int = 0
    skipped_no_fill: int = 0
    skipped_no_order_id: int = 0
    skipped_order_not_targeted: int = 0

    @property
    def total_updates(self) -> int:
        return self.trade_updates + self.fallback_updates


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def order_needs_backfill(order: Optional[dict[str, Any]]) -> bool:
    if not order:
        return False
    gross = _float_or_none(order.get("gross_pnl"))
    return gross is None or gross == 0.0


def fallback_gross_pnl(order: dict[str, Any]) -> Optional[float]:
    net = _float_or_none(order.get("net_pnl"))
    if net is None:
        net = _float_or_none(order.get("realized_pnl"))
    if net is None:
        net = _float_or_none(order.get("realised_pnl"))
    charges = _float_or_none(order.get("charges")) or 0.0
    if net is None or net == 0.0:
        return None
    return round(net + charges, 2)


def _target_order_filter(order_id: str) -> dict[str, Any]:
    return {"id": order_id, **ZERO_GROSS_FILTER}


def backfill_from_trades(db, *, apply: bool, limit: int = 0) -> BackfillStats:
    stats = BackfillStats()
    query = {
        "gross_pnl": {"$exists": True},
        "exit_fill_id": {"$exists": True, "$nin": [None, ""]},
    }
    cursor = db.trades.find(query, {"gross_pnl": 1, "exit_fill_id": 1, "id": 1})
    if limit:
        cursor = cursor.limit(limit)

    for trade in cursor:
        stats.trade_candidates += 1
        gross = _float_or_none(trade.get("gross_pnl"))
        exit_fill_id = trade.get("exit_fill_id")
        # Legacy fills live in db.fills (keyed by `id`); fills booked after the
        # db.fills deprecation live in db.trade_fills (original id under `fill_id`).
        fill = db.fills.find_one({"id": exit_fill_id}, {"order_id": 1, "id": 1})
        if not fill:
            fill = db.trade_fills.find_one({"fill_id": exit_fill_id}, {"order_id": 1})
        if not fill:
            stats.skipped_no_fill += 1
            continue

        order_id = fill.get("order_id")
        if not order_id:
            stats.skipped_no_order_id += 1
            continue

        order = db.orders.find_one(_target_order_filter(order_id), {"id": 1, "gross_pnl": 1})
        if not order:
            stats.skipped_order_not_targeted += 1
            continue

        stats.trade_matched_orders += 1
        if gross is not None and apply:
            result = db.orders.update_one(_target_order_filter(order_id), {"$set": {"gross_pnl": round(gross, 2)}})
            stats.trade_updates += int(result.modified_count or 0)
        elif gross is not None:
            stats.trade_updates += 1

    return stats


def backfill_fallback_orders(db, *, apply: bool, limit: int = 0) -> BackfillStats:
    stats = BackfillStats()
    query = {
        "side": "SELL",
        "$and": [
            ZERO_GROSS_FILTER,
            {
                "$or": [
                    {"net_pnl": {"$exists": True, "$ne": 0}},
                    {"realized_pnl": {"$exists": True, "$ne": 0}},
                    {"realised_pnl": {"$exists": True, "$ne": 0}},
                ]
            },
        ],
    }
    cursor = db.orders.find(query, {"id": 1, "net_pnl": 1, "realized_pnl": 1, "realised_pnl": 1, "charges": 1})
    if limit:
        cursor = cursor.limit(limit)

    for order in cursor:
        gross = fallback_gross_pnl(order)
        if gross is None:
            continue
        stats.fallback_candidates += 1
        if apply:
            result = db.orders.update_one(_target_order_filter(order["id"]), {"$set": {"gross_pnl": gross}})
            stats.fallback_updates += int(result.modified_count or 0)
        else:
            stats.fallback_updates += 1

    return stats


def _combine(a: BackfillStats, b: BackfillStats) -> BackfillStats:
    return BackfillStats(
        trade_candidates=a.trade_candidates + b.trade_candidates,
        trade_matched_orders=a.trade_matched_orders + b.trade_matched_orders,
        trade_updates=a.trade_updates + b.trade_updates,
        fallback_candidates=a.fallback_candidates + b.fallback_candidates,
        fallback_updates=a.fallback_updates + b.fallback_updates,
        skipped_no_fill=a.skipped_no_fill + b.skipped_no_fill,
        skipped_no_order_id=a.skipped_no_order_id + b.skipped_no_order_id,
        skipped_order_not_targeted=a.skipped_order_not_targeted + b.skipped_order_not_targeted,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill gross_pnl on existing closed exit orders")
    parser.add_argument("--apply", action="store_true", help="Write updates. Without this, only report what would change.")
    parser.add_argument("--limit", type=int, default=0, help="Limit trade and fallback scans for a small smoke run.")
    parser.add_argument("--mongo-url", default=os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    parser.add_argument("--db-name", default=os.environ.get("DB_NAME", "quantg"))
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"gross_pnl backfill mode={mode} db={args.db_name} mongo={args.mongo_url}")

    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    db = client[args.db_name]

    trade_stats = backfill_from_trades(db, apply=args.apply, limit=args.limit)
    fallback_stats = backfill_fallback_orders(db, apply=args.apply, limit=args.limit)
    stats = _combine(trade_stats, fallback_stats)

    print(f"trade_candidates={stats.trade_candidates}")
    print(f"trade_matched_orders={stats.trade_matched_orders}")
    print(f"trade_{'updated' if args.apply else 'would_update'}={stats.trade_updates}")
    print(f"fallback_candidates={stats.fallback_candidates}")
    print(f"fallback_{'updated' if args.apply else 'would_update'}={stats.fallback_updates}")
    print(f"skipped_no_fill={stats.skipped_no_fill}")
    print(f"skipped_no_order_id={stats.skipped_no_order_id}")
    print(f"skipped_order_not_targeted={stats.skipped_order_not_targeted}")
    print(f"total_{'updated' if args.apply else 'would_update'}={stats.total_updates}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
