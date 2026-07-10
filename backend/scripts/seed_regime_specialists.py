#!/usr/bin/env python3
"""RAE book rebuild — seed the regime-owning specialist book (idempotent).

Design (CLAUDE.md §18): one specialist per regime, instantiated per underlying,
tagged with the regime(s) it OWNS; the router (RAE-4) activates only the owner on
each day and stands everyone down on HIGH_VOL_CHOP / EVENT / off-regime.

This seeds the RANGE/INSIDE range-seller specialists for NIFTY, BANKNIFTY and
SENSEX, reusing the ONE geometry that passed the reformed regime-conditional judge
(QG-O2 NIFTY Trend-Filtered Put Spread Theta: 3% OTM put credit spread, width 6,
uptrend filter, held to weekly expiry — +₹384/tr on RANGE days, positive IS & OOS).

Seeded status = "paused": present + regime-tagged but NOT firing. The book only
starts routing by regime when the founder sets RAE_ROUTER_ENABLED=true (paper).
CORE_ENGINE_LIVE_ENABLED stays false. TREND delta-1 specialists are a separate
build (need an option-IV-cheap gate — RAE-3c) and are NOT seeded here.

Run in-container:
    docker exec quantg-backend python /app/scripts/seed_regime_specialists.py
    docker exec quantg-backend python /app/scripts/seed_regime_specialists.py --activate  # status=live
"""
import argparse
import copy
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /app

import pymongo  # noqa: E402

RANGE_SELLER_CODE = '''def run(data):
    if len(data) < 60:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:45' or clock > '15:00'):
        return []
    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50
    if not (closes[-1] > ma20 > ma50):
        return []
    return [{
        'date': d['date'], 'action': 'BUY', 'direction': 'CE',
        'setup_type': 'regime_range_seller_put_spread',
        'confidence': 68.0,
        'entry_reason': '%(UL)s uptrend filter passed on a range day; sell OTM put spread',
        'target_R': 1.0, 'initial_stop_R': 1.0, 'trail_after_R': 0.0,
        'max_hold_minutes': 0, 'invalidation_rule': 'weekly_expiry_defined_risk',
        'regime_required': 'range', 'option_selection_preference': 'OTM',
        'signal_version': 'rae-v1', 'strategy_logic_version': 'rae-range-seller-2026-07'
    }]
'''

# per-underlying geometry (short_otm_pct/width proven on NIFTY; capital scaled to
# each underlying's real per-lot spread margin — killswitch-geometry memory).
SPECIALISTS = [
    {"underlying": "NIFTY", "symbol": "NIFTY", "exchange": "NFO",
     "otm_points": 720, "required_capital": 25000.0},
    {"underlying": "BANKNIFTY", "symbol": "BANKNIFTY", "exchange": "NFO",
     "otm_points": 1500, "required_capital": 40000.0},
    {"underlying": "SENSEX", "symbol": "SENSEX", "exchange": "BFO",
     "otm_points": 2400, "required_capital": 60000.0},
]

OWNED_REGIMES = ["RANGE", "INSIDE_QUIET"]
SPECIALIST_ROLE = "range_seller"


def build_doc(template: dict, cfg: dict, activate: bool) -> dict:
    doc = copy.deepcopy(template)
    doc.pop("_id", None)
    ul = cfg["underlying"]
    name = f"RAE {ul} Range Seller (RANGE/INSIDE)"
    doc["id"] = f"rae-range-seller-{ul.lower()}"
    doc["name"] = name
    doc["description"] = (f"RAE regime specialist — sells a defined-risk OTM put credit "
                          f"spread on {ul} only when the day's regime is RANGE/INSIDE "
                          f"(router-gated). Reuses the QG-O2 geometry that passed the "
                          f"regime-conditional judge. Forward-paper; router decides firing.")
    doc["status"] = "live" if activate else "paused"
    doc["manual_paused"] = not activate
    doc["schedule_paused"] = False
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    doc["archived_at"] = None
    doc["python_code"] = RANGE_SELLER_CODE % {"UL": ul}
    doc["required_capital"] = cfg["required_capital"]

    vc = doc.setdefault("visual_config", {})
    vc["symbol"] = cfg["symbol"]
    vc["exchange"] = cfg["exchange"]
    opt = vc.setdefault("options", {})
    opt.update({
        "enabled": True, "underlying": ul, "strike_mode": "OTM_SELL",
        "otm_points": cfg["otm_points"], "expiry_offset": 0, "lots": 1,
        "required_capital": cfg["required_capital"], "product": "NRML",
        "structure": "credit_spread", "spread_width": 6, "short_otm_pct": 0.03,
        "wing_width": 6, "exit_mode": "expiry", "short_delta": 0.12,
        "candle_interval": "5minute",
        # ---- RAE regime tags (read by the router / regime-status route) ----
        "owned_regimes": OWNED_REGIMES, "specialist_role": SPECIALIST_ROLE,
    })
    return doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--activate", action="store_true",
                    help="seed as status=live instead of paused (default paused)")
    args = ap.parse_args()

    db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
                             serverSelectionTimeoutMS=4000)[os.environ.get("DB_NAME", "quantg")]

    template = db.strategies.find_one({"name": {"$regex": "QG-O2"}})
    if not template:
        print("ERROR: QG-O2 template not found — cannot clone geometry.")
        return

    uid = template.get("user_id")
    for cfg in SPECIALISTS:
        doc = build_doc(template, cfg, args.activate)
        doc["user_id"] = uid
        existing = db.strategies.find_one({"name": doc["name"]})
        if existing:
            # preserve runtime counters; refresh definition + tags + status
            db.strategies.update_one({"_id": existing["_id"]}, {"$set": {
                "python_code": doc["python_code"], "visual_config": doc["visual_config"],
                "required_capital": doc["required_capital"], "status": doc["status"],
                "manual_paused": doc["manual_paused"], "description": doc["description"],
                "archived_at": None,
            }})
            print(f"UPDATED  {doc['name']}  [{doc['status']}]  owns={OWNED_REGIMES}")
        else:
            doc.setdefault("_id", str(uuid.uuid4()))
            db.strategies.insert_one(doc)
            print(f"SEEDED   {doc['name']}  [{doc['status']}]  owns={OWNED_REGIMES}")

    print("\nDone. Router gates firing by regime; flip RAE_ROUTER_ENABLED=true (paper) "
          "to activate. CORE_ENGINE_LIVE_ENABLED stays false.")


if __name__ == "__main__":
    main()
