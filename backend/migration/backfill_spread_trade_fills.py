"""Backfill missing canonical trade_fills rows for closed credit spreads.

Usage:
    python migration/backfill_spread_trade_fills.py --date 2026-06-18 --dry-run
    python migration/backfill_spread_trade_fills.py --date 2026-06-18 --apply
"""
from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Optional

from pymongo import MongoClient

IST_OFFSET = timedelta(hours=5, minutes=30)


def trading_day_window_utc(trading_date: str) -> tuple[str, str]:
    day = date.fromisoformat(trading_date)
    ist_midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    start = ist_midnight - IST_OFFSET
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def market_session_window_utc(trading_date: str) -> tuple[str, str]:
    day = date.fromisoformat(trading_date)
    ist_start = datetime(day.year, day.month, day.day, 9, 15, tzinfo=timezone.utc)
    ist_end = datetime(day.year, day.month, day.day, 15, 30, tzinfo=timezone.utc)
    return (ist_start - IST_OFFSET).isoformat(), (ist_end - IST_OFFSET).isoformat()


def _position_closed_time(pos: Dict[str, Any]) -> str:
    return str(pos.get("closed_at") or pos.get("exited_at") or pos.get("updated_at") or pos.get("created_at") or "")


def _missing_spread_positions(db, trading_date: str, user_id: Optional[str] = None) -> Iterable[Dict[str, Any]]:
    start, end = trading_day_window_utc(trading_date)
    query: Dict[str, Any] = {
        "status": "CLOSED",
        "$or": [
            {"asset_type": "option_spread"},
            {"structure": "credit_spread"},
        ],
        "$and": [
            {"$or": [{"closed_at": {"$gte": start, "$lt": end}}, {"updated_at": {"$gte": start, "$lt": end}}]},
        ],
    }
    if user_id:
        query["user_id"] = user_id

    for pos in db.strategy_positions.find(query, {"_id": 0}):
        pos_id = pos.get("id")
        if not pos_id:
            continue
        existing = db.trade_fills.find_one(
            {
                "$or": [
                    {"id": f"tf_spread_{pos_id}"},
                    {"fill_id": f"tf_spread_{pos_id}"},
                    {"position_id": pos_id, "action": {"$in": ["CLOSE", "REDUCE"]}},
                ]
            },
            {"_id": 1},
        )
        if existing:
            continue
        yield pos


def build_spread_trade_fill(pos: Dict[str, Any]) -> Dict[str, Any]:
    pos_id = pos["id"]
    realized = round(float(pos.get("realized_pnl") if pos.get("realized_pnl") is not None else pos.get("pnl") or 0.0), 2)
    gross = round(float(pos.get("gross_pnl") if pos.get("gross_pnl") is not None else realized), 2)
    charges = round(max(0.0, gross - realized), 2)
    closed_at = _position_closed_time(pos)
    return {
        "id": f"tf_spread_{pos_id}",
        "fill_id": f"tf_spread_{pos_id}",
        "position_id": pos_id,
        "user_id": pos.get("user_id"),
        "strategy_id": pos.get("strategy_id"),
        "symbol": pos.get("symbol"),
        "target_symbol": pos.get("target_symbol"),
        "trading_symbol": pos.get("target_symbol"),
        "underlying": pos.get("underlying") or pos.get("symbol"),
        "asset_type": "option_spread",
        "structure": pos.get("structure") or "credit_spread",
        "side": "BUY",
        "qty": int(pos.get("quantity") or pos.get("qty") or 0),
        "price": float(pos.get("exit_value") or pos.get("last_exit_price") or 0.0),
        "charges": charges,
        "gross_pnl": gross,
        "net_pnl": realized,
        "realized_pnl": realized,
        "mode": pos.get("mode"),
        "action": "CLOSE",
        "exit_reason": pos.get("exit_reason"),
        "created_at": closed_at,
        "backfilled": True,
        "backfill_source": "strategy_positions",
    }


def _closing_fills(db, user_id: str, strategy_id: str, trading_date: str) -> list[Dict[str, Any]]:
    start, end = trading_day_window_utc(trading_date)
    return list(
        db.trade_fills.find(
            {
                "user_id": user_id,
                "strategy_id": strategy_id,
                "action": {"$in": ["CLOSE", "REDUCE"]},
                "created_at": {"$gte": start, "$lt": end},
            },
            {"_id": 0, "realized_pnl": 1},
        )
    )


def rebuild_daily_report(db, trading_date: str, user_id: str, apply: bool) -> Dict[str, Any]:
    signal_start, signal_end = market_session_window_utc(trading_date)
    strategies = list(
        db.strategies.find(
            {"user_id": user_id, "status": {"$in": ["live", "paused", "draft"]}},
            {"_id": 0, "id": 1, "name": 1},
        )
    )
    rows = []
    total_realized = 0.0
    total_trades = 0
    for strat in strategies:
        sid = strat.get("id")
        fills = _closing_fills(db, user_id, sid, trading_date)
        realized = round(sum(float(f.get("realized_pnl") or 0.0) for f in fills), 2)
        trade_count = len([f for f in fills if float(f.get("realized_pnl") or 0.0) != 0])
        rows.append({
            "strategy_id": sid,
            "name": strat.get("name", sid),
            "realized_pnl": realized,
            "unrealized_pnl": 0.0,
            "trade_count": trade_count,
        })
        total_realized += realized
        total_trades += trade_count

    signals_fired = db.signals.count_documents({"user_id": user_id, "created_at": {"$gte": signal_start, "$lt": signal_end}})
    signals_filtered = db.signals.count_documents({
        "user_id": user_id,
        "status": "FILTERED",
        "created_at": {"$gte": signal_start, "$lt": signal_end},
    })
    profitable = [s for s in rows if s["realized_pnl"] > 0]
    losing = [s for s in rows if s["realized_pnl"] < 0]
    best = max(profitable, key=lambda s: s["realized_pnl"], default=None)
    worst = min(losing, key=lambda s: s["realized_pnl"], default=None)
    doc = {
        "user_id": user_id,
        "date": trading_date,
        "total_realized_pnl": round(total_realized, 2),
        "total_unrealized_pnl": 0.0,
        "trades_taken": total_trades,
        "signals_fired": signals_fired,
        "signals_filtered": signals_filtered,
        "market_regime": None,
        "best_strategy": {"name": best["name"], "pnl": best["realized_pnl"]} if best else None,
        "worst_strategy": {"name": worst["name"], "pnl": worst["realized_pnl"]} if worst else None,
        "strategies": rows,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reconciled_from": "trade_fills",
    }
    if apply:
        db.daily_reports.update_one({"user_id": user_id, "date": trading_date}, {"$set": doc}, upsert=True)
    return doc


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill spread trade_fills and rebuild daily report")
    parser.add_argument("--date", required=True, help="IST trading date YYYY-MM-DD")
    parser.add_argument("--user-id", default=None)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    db = MongoClient(mongo_url)[db_name]

    positions = list(_missing_spread_positions(db, args.date, args.user_id))
    fill_docs = [build_spread_trade_fill(pos) for pos in positions]
    print(f"missing_spread_trade_fills={len(fill_docs)}")
    for doc in fill_docs:
        print(f"{doc['position_id']} strategy={doc['strategy_id']} pnl={doc['realized_pnl']} created_at={doc['created_at']}")

    if args.apply:
        for doc in fill_docs:
            db.trade_fills.insert_one(doc)

    user_ids = [args.user_id] if args.user_id else sorted({p.get("user_id") for p in positions if p.get("user_id")})
    if not user_ids and args.user_id:
        user_ids = [args.user_id]
    for uid in user_ids:
        report = rebuild_daily_report(db, args.date, uid, apply=args.apply)
        print(f"daily_report user={uid} realized={report['total_realized_pnl']} trades={report['trades_taken']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
