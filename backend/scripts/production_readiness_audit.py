#!/usr/bin/env python3
"""QuantG production readiness and Why-No-Trade audit.

Read-only script. It does not place orders, change strategies, reset state,
or write to MongoDB.

Run from the VPS or local backend environment:

    cd ~/QuantG
    python backend/scripts/production_readiness_audit.py

Optional:
    USER_EMAIL=your@email.com python backend/scripts/production_readiness_audit.py
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pymongo

IST_OFFSET = timedelta(hours=5, minutes=30)
ACTIVE_POSITION_STATUSES = {"RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"}
ORDER_ACTIVE_STATUSES = {"NEW", "PLACED", "OPEN", "PARTIAL_FILL", "PAPER_CREATED", "PENDING_BROKER"}
ORDER_SKIP_STATUSES = {"SKIPPED", "SKIPPED_SIGNAL", "FILTERED", "BLOCKED", "REJECTED"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ist_day_window() -> Tuple[str, str]:
    now_ist = datetime.now(timezone.utc) + IST_OFFSET
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = midnight_ist - IST_OFFSET
    end_utc = start_utc + timedelta(days=1)
    return start_utc.isoformat(), end_utc.isoformat()


def _public(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not doc:
        return {}
    out = dict(doc)
    out.pop("_id", None)
    out.pop("user_id", None)
    return out


def _status_line(ok: bool, label: str, detail: str = "") -> str:
    icon = "PASS" if ok else "FAIL"
    return f"[{icon}] {label}{': ' + detail if detail else ''}"


def _connect():
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    client = pymongo.MongoClient(mongo_url, serverSelectionTimeoutMS=3000)
    client.admin.command("ping")
    return client[db_name]


def _pick_users(db) -> List[Dict[str, Any]]:
    email = os.environ.get("USER_EMAIL")
    query: Dict[str, Any] = {"email": email} if email else {}
    return list(db.users.find(query, {"_id": 0, "password_hash": 0}).limit(50))


def _strategy_stage(db, user_id: str, strategy: Dict[str, Any]) -> Dict[str, Any]:
    sid = strategy.get("id")
    start, end = _ist_day_window()

    latest_signal = db.signals.find_one(
        {"user_id": user_id, "strategy_id": sid},
        sort=[("created_at", pymongo.DESCENDING)],
        projection={"_id": 0},
    )
    latest_order = db.orders.find_one(
        {"user_id": user_id, "strategy_id": sid},
        sort=[("created_at", pymongo.DESCENDING)],
        projection={"_id": 0},
    )
    active_pos = list(db.strategy_positions.find(
        {"user_id": user_id, "strategy_id": sid, "status": {"$in": list(ACTIVE_POSITION_STATUSES)}},
        {"_id": 0},
    ).limit(10))

    status = str(strategy.get("status") or "").lower()
    mode = str(strategy.get("mode") or "paper").lower()
    last_error = strategy.get("last_error")
    last_filter = strategy.get("last_filter_reason")
    last_signals_count = strategy.get("last_signals_count")
    last_data_source = strategy.get("last_data_source")
    latest_candle_age_sec = strategy.get("latest_candle_age_sec")

    stage = "UNKNOWN"
    reason = "No diagnostic marker found."
    next_action = "Check strategy row, signals, and recent backend logs."

    if status != "live":
        stage = "STRATEGY_NOT_LIVE"
        reason = f"Strategy status is {status or 'missing'}."
        next_action = "Enable only one paper strategy after checking readiness."
    elif strategy.get("quarantined") or status == "quarantined":
        stage = "STRATEGY_QUARANTINED"
        reason = strategy.get("quarantine_reason") or last_filter or "Strategy was quarantined."
        next_action = "Fix strategy signal behavior, then clear quarantine deliberately."
    elif last_error:
        text = str(last_error)
        if "Upstox" in text or "price history" in text or "gateway" in text:
            stage = "NO_PRICE_HISTORY"
            next_action = "Reconnect Upstox, restart market data, verify fresh candles."
        elif "CONTRACT" in text.upper() or "contract" in text.lower():
            stage = "CONTRACT_RESOLUTION_FAILED"
            next_action = "Check Upstox instrument master, expiry, strike and segment permission."
        else:
            stage = "STRATEGY_ERROR"
            next_action = "Open strategy code/runtime error and fix root cause."
        reason = text
    elif active_pos:
        stage = "POSITION_OPEN"
        reason = f"{len(active_pos)} active strategy position(s) exist."
        next_action = "Wait for exit logic or manually unwind if this is stale."
    elif latest_order:
        order_status = str(latest_order.get("status") or "").upper()
        if order_status in ORDER_ACTIVE_STATUSES:
            stage = "ORDER_CREATED"
            reason = latest_order.get("status_message") or order_status
            next_action = "Check order lifecycle and whether fill/position was created."
        elif order_status in ORDER_SKIP_STATUSES:
            stage = "PREFLIGHT_SKIPPED"
            reason = latest_order.get("reject_reason") or latest_order.get("status_message") or latest_order.get("error_message") or order_status
            next_action = "Fix the exact preflight reason; do not force orders."
        elif "FILLED" in order_status or order_status == "COMPLETE":
            stage = "PAPER_FILLED" if latest_order.get("mode") == "paper" else "ORDER_FILLED"
            reason = f"Latest order filled at {latest_order.get('price')}."
            next_action = "Verify strategy_positions, positions and wallet/P&L reconciliation."
        else:
            stage = f"ORDER_{order_status or 'UNKNOWN'}"
            reason = latest_order.get("status_message") or latest_order.get("error_message") or "Order exists with unclear state."
            next_action = "Inspect order_events and order document."
    elif latest_signal:
        sig_status = str(latest_signal.get("status") or "").upper()
        if sig_status == "PENDING":
            stage = "SIGNAL_PENDING"
            reason = "Strategy emitted a signal but signal_manager has not processed it yet."
            next_action = "Check signal_manager_task status and runner_locks."
        elif sig_status in ORDER_SKIP_STATUSES:
            stage = "SIGNAL_FILTERED_BY_MANAGER"
            reason = latest_signal.get("rejection_reason") or latest_signal.get("rejection_detail") or sig_status
            next_action = "Fix strategy limits/conflicts/contract/LTP reason shown here."
        elif sig_status == "PROCESSED":
            stage = "SIGNAL_PROCESSED_NO_ORDER_FOUND"
            reason = "Signal processed but no linked latest order was found."
            next_action = "Check order_id on signal and idempotency key handling."
        else:
            stage = f"SIGNAL_{sig_status or 'UNKNOWN'}"
            reason = latest_signal.get("rejection_reason") or sig_status
            next_action = "Inspect signal document."
    elif last_signals_count == 0:
        stage = "NO_SIGNAL"
        reason = last_filter or "Strategy scanned but emitted no current signal."
        next_action = "This may be normal. Backtest and check latest candles."
    elif last_filter:
        if "Market closed" in str(last_filter):
            stage = "MARKET_CLOSED"
            next_action = "Wait for the correct NSE/MCX session."
        elif "Signal filtered" in str(last_filter):
            stage = "SIGNAL_FILTERED"
            next_action = "Lower noise or adjust strategy only after confirming real data."
        else:
            stage = "FILTERED"
            next_action = "Read filter reason and verify if expected."
        reason = str(last_filter)
    else:
        stage = "SCANNING"
        reason = "Strategy appears to be scanning but no signal/order record is present."
        next_action = "Check runner task, latest_evaluated_at and candle freshness."

    return {
        "strategy_id": sid,
        "name": strategy.get("name"),
        "status": status,
        "mode": mode,
        "stage": stage,
        "reason": reason,
        "next_action": next_action,
        "last_evaluated_at": strategy.get("last_evaluated_at"),
        "last_data_source": last_data_source,
        "latest_candle_age_sec": latest_candle_age_sec,
        "latest_signal": _public(latest_signal),
        "latest_order": _public(latest_order),
        "active_positions": [_public(p) for p in active_pos],
        "today_signal_count": db.signals.count_documents({"user_id": user_id, "strategy_id": sid, "created_at": {"$gte": start, "$lt": end}}),
        "today_order_count": db.orders.count_documents({"user_id": user_id, "strategy_id": sid, "created_at": {"$gte": start, "$lt": end}}),
    }


def _readiness_summary(db, user: Dict[str, Any]) -> Dict[str, Any]:
    user_id = user["id"]
    start, end = _ist_day_window()
    strategies = list(db.strategies.find({"user_id": user_id}, {"_id": 0}).sort("created_at", -1))
    live_paper = [s for s in strategies if s.get("status") == "live" and str(s.get("mode") or "paper").lower() == "paper"]
    active_positions = db.strategy_positions.count_documents({"user_id": user_id, "status": {"$in": list(ACTIVE_POSITION_STATUSES)}})
    pending_signals = db.signals.count_documents({"user_id": user_id, "status": "PENDING"})
    stale_pending_signals = db.signals.count_documents({
        "user_id": user_id,
        "status": "PENDING",
        "created_at": {"$lt": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()},
    })
    skipped_today = db.signals.count_documents({
        "user_id": user_id,
        "processed_at": {"$gte": start, "$lt": end},
        "status": {"$in": ["FILTERED", "REJECTED", "SKIPPED_SIGNAL", "BLOCKED"]},
    })
    unknown_live_orders = db.orders.count_documents({"user_id": user_id, "mode": "live", "status": "UNKNOWN_NEEDS_REVIEW"})
    paper_orders_today = list(db.orders.find({"user_id": user_id, "mode": "paper", "created_at": {"$gte": start, "$lt": end}}, {"_id": 0}).sort("created_at", -1).limit(100))
    suspicious_fake_prices = [o for o in paper_orders_today if float(o.get("price") or 0) in {34.5, 34.50, 34.51} and "upstox" not in str(o.get("price_source") or "").lower()]
    wallet = db.paper_wallets.find_one({"user_id": user_id}, {"_id": 0}) or {}
    settings = user.get("settings") or user

    checks = [
        {"name": "paper_mode_enabled", "ok": bool(settings.get("paper_mode", True)), "detail": f"paper_mode={settings.get('paper_mode', True)}"},
        {"name": "one_or_two_live_paper_strategies", "ok": 1 <= len(live_paper) <= 2, "detail": f"live_paper_strategies={len(live_paper)}"},
        {"name": "no_unknown_live_orders", "ok": unknown_live_orders == 0, "detail": f"unknown_live_orders={unknown_live_orders}"},
        {"name": "no_stale_pending_signals", "ok": stale_pending_signals == 0, "detail": f"pending={pending_signals}, stale_pending={stale_pending_signals}"},
        {"name": "no_suspicious_fake_option_fills_today", "ok": len(suspicious_fake_prices) == 0, "detail": f"suspicious_fake_prices={len(suspicious_fake_prices)}"},
        {"name": "paper_wallet_exists", "ok": bool(wallet), "detail": f"balance={wallet.get('balance')}"},
        {"name": "active_position_count_visible", "ok": True, "detail": f"active_strategy_positions={active_positions}"},
        {"name": "skipped_reasons_visible", "ok": True, "detail": f"skipped_signals_today={skipped_today}"},
    ]
    return {
        "user_email": user.get("email"),
        "user_id": user_id,
        "generated_at": _now_iso(),
        "status": "PASS" if all(c["ok"] for c in checks) else "BLOCKED",
        "checks": checks,
        "strategies": [_strategy_stage(db, user_id, s) for s in strategies],
    }


def main() -> int:
    db = _connect()
    users = _pick_users(db)
    if not users:
        print("No users found. Set USER_EMAIL or check DB_NAME/MONGO_URL.")
        return 2

    print("=" * 90)
    print("QUANTG PRODUCTION READINESS AUDIT - READ ONLY")
    print("=" * 90)
    print(f"Generated at UTC: {_now_iso()}")
    print(f"Users audited: {len(users)}")

    exit_code = 0
    for user in users:
        summary = _readiness_summary(db, user)
        if summary["status"] != "PASS":
            exit_code = 1
        print("\n" + "-" * 90)
        print(f"USER: {summary['user_email']} | STATUS: {summary['status']}")
        print("-" * 90)
        for check in summary["checks"]:
            print(_status_line(check["ok"], check["name"], check["detail"]))
        print("\nWHY NO TRADE / STRATEGY STAGES")
        for row in summary["strategies"]:
            print("\n" + f"Strategy: {row['name']} [{row['strategy_id']}]")
            print(f"  status/mode : {row['status']} / {row['mode']}")
            print(f"  stage       : {row['stage']}")
            print(f"  reason      : {row['reason']}")
            print(f"  next action : {row['next_action']}")
            print(f"  data source : {row.get('last_data_source')} | candle age: {row.get('latest_candle_age_sec')}")
            print(f"  today       : signals={row['today_signal_count']} orders={row['today_order_count']} active_positions={len(row['active_positions'])}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
