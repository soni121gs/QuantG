#!/usr/bin/env python3
"""Seed the far-OTM tail-hedge (convex crash insurance) — 2026-08-01. Idempotent.

The validated long-vol structure is a far-OTM long put (core/long_vol_tail.py: COVID
trade paid +Rs54,823, ~self-funding over a crash window). The live single-leg path
cannot BUY a put (directional-buyer / credit-seller only), so the deployable vehicle
is a bear PUT DEBIT spread whose LONG leg sits far OTM — achieved with the EXISTING
build_debit_spread by passing a LOW short_delta (->long_delta ~0.15). No builder
change; proven by tests/test_far_otm_tail_hedge.py.

Held to expiry (exit_mode="expiry") so it rides OVERNIGHT — gap-downs are the dominant
index tail. Anti-pyramiding holds exactly one hedge; it rolls when the prior settles.
Debit spreads are exempt from the §21 cost/reachability laws (they are buyers). A build
failure stands down cleanly (returns ok=False) — no error path.

Sized tiny (insurance): required_capital small -> ~1 lot, defined max loss = the debit.
CORE_ENGINE_LIVE_ENABLED stays false (paper). Clones schema from the IDX NIFTY Long-Gamma
debit sleeve so the doc shape is exactly what the live debit path expects.

Run in-container:
    docker exec quantg-backend python /app/scripts/seed_tail_hedge.py            # dry-run
    docker exec quantg-backend python /app/scripts/seed_tail_hedge.py --apply
    docker exec quantg-backend python /app/scripts/seed_tail_hedge.py --apply --arm   # + arm for Mon wake
"""
import argparse
import copy
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /app

import pymongo  # noqa: E402
from core.strategy_registry import validate_strategy_doc  # noqa: E402

HEDGE_NAME = "Tail Hedge NIFTY Far-OTM Put Spread"

# Always-on: establish/maintain the far-OTM put-spread hedge whenever none is held.
# action=SELL -> _direction=bearish -> bear PUT debit spread (long PE). Anti-pyramiding
# keeps exactly one open; it re-fires only after the prior hedge settles at expiry.
TAIL_HEDGE_CODE = '''def run(data):
    if len(data) < 20:
        return []
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:20' or clock > '15:00'):
        return []
    return [{
        'date': d['date'], 'action': 'SELL', 'direction': 'PE',
        'setup_type': 'tail_hedge_put_debit_spread', 'confidence': 60.0,
        'entry_reason': 'always-on far-OTM put debit spread - convex crash insurance',
        'regime_required': 'any', 'option_selection_preference': 'OTM',
        'signal_version': 'tailhedge-v1', 'strategy_logic_version': 'tailhedge-2026-08'
    }]
'''

HEDGE_OPTIONS = {
    "enabled": True, "underlying": "NIFTY", "structure": "debit_spread",
    "strike_mode": "OTM_BUY", "short_delta": 0.15,   # -> long put ~far OTM (delta 0.15)
    "spread_width": 4, "wing_width": 4,
    "exit_mode": "expiry",                            # hold OVERNIGHT, roll at expiry
    "lots": 1, "expiry_offset": 0, "product": "NRML", "candle_interval": "5minute",
    "specialist_role": "tail_hedge",
    "owned_regimes": ["RANGE", "INSIDE_QUIET", "HIGH_VOL_CHOP", "EVENT", "TREND_UP", "TREND_DOWN"],
    "required_capital": 4000.0,
}
HEDGE_RISK = {"exit_mode": "hold_to_expiry", "max_hold_days": 8}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--arm", action="store_true", help="arm for the 09:15 IST Monday wake")
    args = ap.parse_args()
    db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
                             serverSelectionTimeoutMS=5000)[os.environ.get("DB_NAME", "quantg")]

    template = db.strategies.find_one({"name": {"$regex": "IDX NIFTY Long-Gamma"}})
    if not template:
        print("ERROR: IDX NIFTY Long-Gamma debit sleeve not found — cannot clone schema.")
        return 1

    doc = copy.deepcopy(template)
    doc.pop("_id", None)
    doc["id"] = "tail-hedge-nifty-farotm-putspread"
    doc["name"] = HEDGE_NAME
    doc["description"] = ("Far-OTM put DEBIT spread = convex crash insurance (long-vol §18). "
                          "Held to expiry so it rides overnight gap-downs; small defined-risk "
                          "debit, rolled each expiry. Judge on book-drawdown reduction, not "
                          "standalone P&L (insurance costs money on average).")
    doc["python_code"] = TAIL_HEDGE_CODE
    doc["required_capital"] = HEDGE_OPTIONS["required_capital"]
    armed = bool(args.arm)
    doc["status"] = "paused"
    doc["schedule_paused"] = armed         # True -> 09:15 Mon wake picks it up
    doc["manual_paused"] = not armed
    doc["archived_at"] = None
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    doc["founder_forced_live"] = True

    vc = doc.setdefault("visual_config", {})
    vc["symbol"] = "NIFTY"
    vc["exchange"] = "NFO"
    vc["options"] = dict(HEDGE_OPTIONS)
    vc["risk"] = dict(HEDGE_RISK)

    v = validate_strategy_doc(doc)
    print(f"coherence: ok={v.ok} errors={v.errors} warnings={v.warnings}")
    if not v.ok:
        print("ABORT — coherence errors.")
        return 1

    existing = db.strategies.find_one({"name": HEDGE_NAME})
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] {'UPDATE' if existing else 'SEED'} {HEDGE_NAME} "
          f"status={doc['status']} armed={armed} debit far-OTM put spread")
    if args.apply:
        if existing:
            db.strategies.update_one({"_id": existing["_id"]}, {"$set": {
                "python_code": doc["python_code"], "visual_config": doc["visual_config"],
                "required_capital": doc["required_capital"], "description": doc["description"],
                "status": doc["status"], "schedule_paused": doc["schedule_paused"],
                "manual_paused": doc["manual_paused"], "archived_at": None,
                "founder_forced_live": True,
            }})
        else:
            doc.setdefault("_id", str(uuid.uuid4()))
            doc.setdefault("user_id", template.get("user_id"))
            db.strategies.insert_one(doc)
        print("written.")
    else:
        print("re-run with --apply (add --arm to wake it Monday).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
