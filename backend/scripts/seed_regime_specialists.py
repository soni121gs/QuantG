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

TREND_DELTA1_CODE = '''def run(data):
    if len(data) < 55:
        return []
    closes = [float(d.get('close') or 0) for d in data]
    highs = [float(d.get('high') or d.get('close') or 0) for d in data]
    lows = [float(d.get('low') or d.get('close') or 0) for d in data]
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and (clock < '09:45' or clock > '14:45'):
        return []
    ma20 = sum(closes[-20:]) / 20
    ma50 = sum(closes[-50:]) / 50
    c = closes[-1]
    path = sum(abs(closes[-i] - closes[-i - 1]) for i in range(1, 11)) or 1e-9
    eff = abs(closes[-1] - closes[-11]) / path       # trending, not chopping
    # TIGHTENED (RAE-3c): higher efficiency floor (0.45->0.55), a 30-bar breakout
    # (was 20), AND a 3-step monotonic momentum confirm — cuts range-fakeout fires.
    mom_up = closes[-1] > closes[-3] > closes[-5]
    mom_dn = closes[-1] < closes[-3] < closes[-5]
    up = c > ma20 > ma50 and c >= max(highs[-31:-1]) and eff >= 0.55 and mom_up
    dn = c < ma20 < ma50 and c <= min(lows[-31:-1]) and eff >= 0.55 and mom_dn
    if not (up or dn):
        return []
    direction = 'CE' if up else 'PE'
    return [{
        'date': d['date'], 'action': 'BUY', 'direction': direction,
        'setup_type': 'regime_trend_delta1',
        'confidence': 72.0,
        'entry_reason': '%(UL)s strong trend breakout + MA alignment; buy deep-ITM ' + direction,
        'target_R': 2.5, 'initial_stop_R': 1.0, 'trail_after_R': 1.0,
        'max_hold_minutes': 0, 'invalidation_rule': 'trend_break',
        'regime_required': 'trend', 'option_selection_preference': 'ITM',
        'signal_version': 'rae-v1', 'strategy_logic_version': 'rae-trend-delta1-2026-07'
    }]
'''

# Specialist templates. Each row is instantiated per underlying; the router
# activates it only on the regime(s) it owns and stands it down elsewhere.
TEMPLATES = {
    "range_seller": {
        "code": RANGE_SELLER_CODE, "role": "range_seller",
        "owned": ["RANGE", "INSIDE_QUIET"], "structure": "credit_spread",
        "name": "Range Seller (RANGE/INSIDE)",
        "desc": ("sells a defined-risk OTM put credit spread only on RANGE/INSIDE days "
                 "(router-gated). Reuses the QG-O2 geometry that passed the "
                 "regime-conditional judge."),
        "options": {"strike_mode": "OTM_SELL", "structure": "credit_spread",
                    "spread_width": 6, "short_otm_pct": 0.03, "wing_width": 6,
                    "exit_mode": "expiry", "short_delta": 0.12},
        "risk": {},
    },
    "trend_delta1": {
        "code": TREND_DELTA1_CODE, "role": "trend_delta1",
        "owned": ["TREND_UP", "TREND_DOWN"], "structure": "single_leg",
        "name": "Trend Delta-1 (TREND)",
        "desc": ("buys a DEEP-ITM (delta ~0.7) single leg — CE on a confirmed up-trend, "
                 "PE on a down-trend — only on TREND days at router confidence >=0.90, "
                 "gated by IV-cheap (RAE-3c) + a tightened breakout/efficiency/momentum "
                 "filter. Low-theta directional; the fix for why every OTM buyer died. "
                 "Forward-paper (trend days are rare; gate not OOS-provable on this sample)."),
        "options": {"strike_mode": "ITM_BUY", "structure": "single_leg",
                    "itm_offset_pct": 0.02, "option_selection_preference": "ITM1",
                    "exit_mode": "", "trend_iv_gate": True, "trend_iv_gate_min_cheap": 0.0},
        "risk": {"target_pct": 60.0, "stoploss_pct": 25.0, "max_hold_days": 2},
    },
}

# per-underlying capital (real per-lot margin — killswitch-geometry memory).
UNDERLYINGS = [
    {"underlying": "NIFTY", "symbol": "NIFTY", "exchange": "NFO",
     "cap": {"range_seller": 25000.0, "trend_delta1": 35000.0}},
    {"underlying": "BANKNIFTY", "symbol": "BANKNIFTY", "exchange": "NFO",
     "cap": {"range_seller": 40000.0, "trend_delta1": 55000.0}},
    {"underlying": "SENSEX", "symbol": "SENSEX", "exchange": "BFO",
     "cap": {"range_seller": 60000.0, "trend_delta1": 90000.0}},
]


def build_doc(template: dict, tpl: dict, cfg: dict, activate: bool) -> dict:
    doc = copy.deepcopy(template)
    doc.pop("_id", None)
    ul = cfg["underlying"]
    role = tpl["role"]
    cap = cfg["cap"][role]
    name = f"RAE {ul} {tpl['name']}"
    doc["id"] = f"rae-{role.replace('_', '-')}-{ul.lower()}"
    doc["name"] = name
    doc["description"] = f"RAE regime specialist — {ul} {tpl['desc']} Router decides firing."
    doc["status"] = "live" if activate else "paused"
    doc["manual_paused"] = not activate
    doc["schedule_paused"] = False
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    doc["archived_at"] = None
    doc["python_code"] = tpl["code"] % {"UL": ul}
    doc["required_capital"] = cap

    vc = doc.setdefault("visual_config", {})
    vc["symbol"] = cfg["symbol"]
    vc["exchange"] = cfg["exchange"]
    opt = vc.setdefault("options", {})
    opt.update({
        "enabled": True, "underlying": ul, "expiry_offset": 0, "lots": 1,
        "required_capital": cap, "product": "NRML", "candle_interval": "5minute",
        "owned_regimes": tpl["owned"], "specialist_role": role,
    })
    opt.update(tpl["options"])
    risk = vc.setdefault("risk", {})
    risk.update(tpl["risk"])
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
    for cfg in UNDERLYINGS:
        for role, tpl in TEMPLATES.items():
            doc = build_doc(template, tpl, cfg, args.activate)
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
                verb = "UPDATED"
            else:
                doc.setdefault("_id", str(uuid.uuid4()))
                db.strategies.insert_one(doc)
                verb = "SEEDED "
            print(f"{verb}  {doc['name']}  [{doc['status']}]  owns={tpl['owned']}")

    print("\nDone. Router gates firing by regime; flip RAE_ROUTER_ENABLED=true (paper) "
          "to activate. CORE_ENGINE_LIVE_ENABLED stays false.")


if __name__ == "__main__":
    main()
