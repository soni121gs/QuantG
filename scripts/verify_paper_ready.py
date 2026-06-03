from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient


IST_OFFSET = timedelta(hours=5, minutes=30)


def trading_day_window_ist() -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    ist_now = now_utc + IST_OFFSET
    ist_midnight = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = ist_midnight - IST_OFFSET
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def http_json(url: str, token: str | None = None) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
        return {"status_code": resp.status, "body": json.loads(raw) if raw else None}


def is_34xx(value) -> bool:
    try:
        price = float(value or 0)
    except (TypeError, ValueError):
        return False
    return 34.0 <= price < 35.0


def has_real_upstox_metadata(order: dict) -> bool:
    source = str(order.get("price_source") or "").lower()
    token = str(order.get("instrument_key") or order.get("instrument_token") or "")
    received_at = order.get("price_received_at") or order.get("price_timestamp")
    return source in {"upstox-cache", "upstox-rest-quote"} and "|" in token and bool(received_at)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify QuantG strict paper readiness for one account.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--api-base", default=os.environ.get("QUANTG_API_BASE", "https://www.quantgtrade.com/api"))
    parser.add_argument("--mongo-url", default=os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
    parser.add_argument("--db-name", default=os.environ.get("DB_NAME", "quantg"))
    parser.add_argument("--token", default=os.environ.get("QUANTG_API_TOKEN"))
    parser.add_argument("--since", default=os.environ.get("STRICT_PAPER_READY_SINCE"), help="UTC ISO lower bound for order/signal checks; defaults to current IST trading day.")
    args = parser.parse_args()

    failures: list[str] = []
    evidence: dict = {"email": args.email}

    try:
        evidence["backend_health"] = http_json(args.api_base.rstrip("/") + "/")
    except Exception as exc:
        failures.append(f"backend health failed: {exc}")

    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=5000)
    db = client[args.db_name]
    user = db.users.find_one({"email": args.email}, {"_id": 0})
    if not user:
        failures.append("user not found")
        print(json.dumps({"ok": False, "failures": failures, "evidence": evidence}, indent=2, default=str))
        return 1
    uid = user["id"]
    start, end = trading_day_window_ist()
    if args.since:
        start = args.since
    evidence["user"] = {
        "id": uid,
        "paper_mode": bool(user.get("paper_mode", True)),
        "allow_simulated_prices": bool(user.get("allow_simulated_prices")),
        "execution_broker": user.get("execution_broker"),
        "data_broker": user.get("data_broker"),
    }
    if not evidence["user"]["paper_mode"]:
        failures.append("paper_mode is false")

    orders = list(db.orders.find({"user_id": uid, "mode": "paper", "created_at": {"$gte": start, "$lt": end}}, {"_id": 0}))
    bad_34 = [o for o in orders if is_34xx(o.get("price")) and not has_real_upstox_metadata(o)]
    missing_source = [o for o in orders if str(o.get("status") or "").upper() in {"PAPER_CREATED", "PAPER_FILLED"} and not o.get("price_source")]
    simulated_positions = list(db.positions.find({"user_id": uid, "mode": "paper", "$or": [{"price_source": {"$regex": "mock|simulated|fallback", "$options": "i"}}, {"source": {"$regex": "mock|simulated|fallback", "$options": "i"}}]}, {"_id": 0}))
    recent_skips = list(db.signals.find({"user_id": uid, "processed_at": {"$gte": start, "$lt": end}, "status": {"$in": ["FILTERED", "REJECTED", "SKIPPED_SIGNAL", "BLOCKED"]}}, {"_id": 0, "id": 1, "status": 1, "rejection_reason": 1}).sort("processed_at", -1).limit(20))
    active_strategies = db.strategies.count_documents({"user_id": uid, "mode": "paper", "status": "live"})
    readiness_fields = db.strategies.count_documents({"user_id": uid, "mode": "paper", "$or": [{"last_signal_validated": {"$exists": True}}, {"last_skip_reason_code": {"$exists": True}}, {"quarantine_reason": {"$exists": True}}]})

    evidence["checked_since"] = start
    evidence["paper_orders_checked"] = len(orders)
    evidence["bad_34xx_orders_checked"] = len(bad_34)
    evidence["paper_orders_missing_price_source"] = len(missing_source)
    evidence["simulated_positions"] = len(simulated_positions)
    evidence["recent_skips_with_reason"] = len([s for s in recent_skips if s.get("rejection_reason")])
    evidence["active_paper_strategies"] = active_strategies
    evidence["strategies_with_readiness_fields"] = readiness_fields

    if bad_34:
        failures.append("paper orders today include 34.xx without real Upstox metadata")
    if missing_source:
        failures.append("paper filled/created orders missing price_source")
    if simulated_positions:
        failures.append("paper positions appear to come from fake/simulated source")
    if recent_skips and evidence["recent_skips_with_reason"] != len(recent_skips):
        failures.append("recent skipped signals missing clear reason_code")

    if args.token:
        for path in ("/paper-readiness", "/strategy-readiness"):
            try:
                evidence[path] = http_json(args.api_base.rstrip("/") + path, args.token)
            except Exception as exc:
                failures.append(f"{path} failed: {exc}")
    else:
        evidence["readiness_api_auth"] = "skipped: set QUANTG_API_TOKEN to verify authenticated readiness endpoints"

    ok = not failures
    print(json.dumps({"ok": ok, "failures": failures, "evidence": evidence}, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
