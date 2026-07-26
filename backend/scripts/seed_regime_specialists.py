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
    if len(data) < 52:
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
        # Intraday booking, NOT hold-to-expiry: mirrors the QG-O1/O4 07-09 fix
        # (8d46076). 2026-07-18 (Hermes Diagnostician static.reward_risk_geometry,
        # HIGH): TP 0.35 / SL 1.5 needed an 81% breakeven WR — live ran BANKNIFTY
        # 0/5 (-Rs7,305), SENSEX 2/4, well short of that. Re-derived to TP 0.50 /
        # SL 0.90 -> breakeven ~64%, an achievable seller WR. Stays router-gated +
        # paused pending an OOS re-run on the new shape. time_exit/cooldown unchanged
        # (120m / 15m). Forward-paper judges; all reversible.
        # 2026-07-21 (measured on the LIVE chain, not assumed): short_delta 0.12
        # clears the ERP cost floor in ZERO of the geometries probed — NIFTY w6
        # multiple 2.05, BANKNIFTY w6 2.18, SENSEX w6 1.27, all below the required
        # 3.0. At delta 0.30 the same width-6 pays multiples 6.41 / 5.87 / 3.94.
        # Live record agreed: RAE NIFTY 1 trade, BANKNIFTY -Rs8,837/8, SENSEX
        # -Rs2,981/6, with 0, 0 and 2 price-driven exits respectively.
        # Width 6 -> 4 keeps per-lot defined risk near Rs10,000 at the higher delta.
        # time_exit 120 -> 300: at 120 minutes and ~2 DTE theta supplied only 0.16
        # of credit against a 0.50 target (32% of it), so the clock decided the
        # trade. At 300 minutes theta supplies ~0.80 of the target.
        "options": {"strike_mode": "OTM_SELL", "structure": "credit_spread",
                    "spread_width": 4, "short_otm_pct": 0.012, "wing_width": 4,
                    "exit_mode": "", "short_delta": 0.30, "target_dte_days": 3,
                    # TP 0.50 -> 0.45: at a realistic ~3 DTE mid-week weekly entry a
                    # 300-minute hold lets theta supply 0.53 of a 0.50 target but
                    # 0.59 of a 0.45 one. Breakeven WR moves 0.643 -> 0.667, still
                    # an achievable seller win rate. SENSEX (Thursday expiry, ~2 DTE)
                    # is overridden below where theta has more room.
                    "credit_tp_frac": 0.45, "credit_sl_mult": 0.90},
        "risk": {"exit_mode": "signal_or_tp_sl_trailing", "time_exit_minutes": 300,
                 "trail_trigger_pct": 4.0, "trail_step_pct": 2.0},
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
# 2026-07-21: range_seller caps re-derived for the new width-4 / delta-0.30
# geometry. `lots_for_risk` sizes as budget // (max_loss_per_unit * lot_size), so
# a cap left at the old width-6 / delta-0.12 level would SIZE UP hard on the now
# much narrower per-lot risk: NIFTY 25000/10004 = 2 lots, BANKNIFTY 40000/9446 = 4
# lots, SENSEX 60000/6188 = 9 lots. Per-lot max loss measured on the live chain
# (NIFTY Rs10,004, BANKNIFTY Rs9,446, SENSEX Rs6,188) -> cap each at ~1 lot until
# the re-cut shape has forward-paper evidence. trend_delta1 caps are unchanged
# (single-leg ITM buyers, untouched by this credit-spread work).
UNDERLYINGS = [
    {"underlying": "NIFTY", "symbol": "NIFTY", "exchange": "NFO",
     "cap": {"range_seller": 11000.0, "trend_delta1": 35000.0}},
    {"underlying": "BANKNIFTY", "symbol": "BANKNIFTY", "exchange": "NFO",
     "cap": {"range_seller": 10500.0, "trend_delta1": 55000.0}},
    # SENSEX range_seller cap 10500 -> 13000 for the 2026-07-26 width 6 -> 8 re-cut:
    # per-lot max loss rises to ~Rs12,600, and `lots_for_risk` floor-divides, so the
    # old cap would yield 0 lots — a silent stand-down indistinguishable from a veto.
    {"underlying": "SENSEX", "symbol": "SENSEX", "exchange": "BFO",
     "cap": {"range_seller": 13000.0, "trend_delta1": 90000.0}},
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
    # SENSEX expires Thursday, so a mid-week entry sits at ~2 DTE rather than the
    # ~3 DTE of the NIFTY/BANKNIFTY Tuesday weekly. That extra decay room lets the
    # take-profit sit at 0.50 of credit and still be theta-reachable inside the
    # 300-minute hold (reachability 0.80 vs 0.59), with a lower breakeven WR.
    # SENSEX also runs a WIDER wing (6 vs 4). Its lot size is only 20, so rupee
    # credit per lot is small and a width-4 spread lands right on the cost-floor
    # boundary (measured TP Rs884 against a Rs900 floor) — it would trade only on
    # the richest days and silently stand down otherwise. Width 6 collects
    # materially more (measured multiple 3.94 vs 3.02) while keeping per-lot max
    # loss near Rs9,600, so the cap moves with it.
    # 2026-07-26: width 6 -> 8 and a 330-minute hold. SENSEX expires THURSDAY, so a
    # Monday entry faces 3 DTE, and at width 6 NO take-profit satisfied both §21 laws
    # at once — tp 0.50 failed the cost floor (2.77x, measured on real 3-DTE fills)
    # AND failed reachability (0.53); lowering tp fixed reachability and made the cost
    # floor worse (0.45 -> 2.49x). The laws pull in opposite directions, so the only
    # lever that helps both is a wider wing: it raises the credit without touching
    # reachability. Verified on real 3-DTE width-800 SENSEX fills — cost multiples
    # 3.29-3.67, reachability(tp 0.50, hold 330) 0.587. Cap rises with the wing
    # (§21.4): per-lot max loss ~Rs9,122 -> ~Rs12,600, so the old Rs10,500 would size
    # to ZERO lots. UNVALIDATED shape; owes a judge run + forward-paper (§13.5).
    if role == "range_seller" and ul == "SENSEX":
        opt.update({"target_dte_days": 2, "credit_tp_frac": 0.50,
                    "spread_width": 8, "wing_width": 8})
    risk = vc.setdefault("risk", {})
    risk.update(tpl["risk"])
    # AFTER the template risk merge above, which would otherwise clobber this back
    # to the shared 300-minute hold.
    if role == "range_seller" and ul == "SENSEX":
        risk["time_exit_minutes"] = 330
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
