#!/usr/bin/env python3
"""Seed the HELD-TO-EXPIRY defined-risk sleeve + restore QG-O1 to its validated
form — 2026-08-03. Idempotent, dry-run by default.

WHY (measured, not assumed)
---------------------------
A structure-shootout over 1,869 real bhavcopy days (2019-01-01..2026-07-30,
includes COVID-2020) held the SIGNAL constant — same entry days, same underlying,
same window, corrected friction — and varied ONLY the structure/exit:

    structure                        hold      n     WR     breakeven   expectancy
    credit spread w=4                5 days   221   61.1%     64.4%      -Rs282
    credit spread w=10               5 days   216   65.7%     68.1%      -Rs338
    debit  spread w=10               5 days   371   40.4%     42.7%      -Rs247
    single leg                       5 days   374   37.2%     41.1%      -Rs339
    credit spread w=10          TO EXPIRY     337   70.6%     66.8%      +Rs661
    debit  spread w=10          TO EXPIRY     337   41.2%     35.5%      +Rs908

EVERY early-exit row loses. BOTH held-to-expiry rows beat their own breakeven, and
they do it from opposite directions (the credit row on win rate, the debit row on
payoff b=1.82). The discriminator is the EXIT, not credit-vs-debit: each early exit
pays round-trip friction and truncates the distribution before the payoff exists.

This matches the live book exactly. Of 631 closed trades, 86% of exits were
clock-driven (CLAUDE.md §21.2) and the four biggest loss pools are all forced
exits: stop-loss -Rs41,294, killswitch -Rs24,859, spread-sl -Rs17,712,
spread-time-exit -Rs17,067.

And the punchline: per §22.3 hold-to-expiry has NEVER EXECUTED — 0 overnight holds
across 283 closed spreads, one 'expiry-settlement' in all history. The 15:26 EOD
backstop closed them every day. The one structure that ever passed a QuantG judge
(QG-O1's §15.5 CANDIDATE_EDGE) is a structure the platform has never actually run.
The backstop exemption was fixed in 38be295; what was still missing is a strategy
configured to use it.

WHAT THIS SEEDS
---------------
1. "HTE NIFTY Defined-Risk Put Spread" — NEW. The tested geometry: ~3% OTM short
   strike, width 10, entered 5-15 DTE, HELD TO EXPIRY. No clock exit, no percentage
   stop — the bought wing IS the stop and max loss is known and reserved at entry.
2. QG-O1 restored to exit_mode="expiry". On 2026-07-09 it was converted to an
   intraday TP/SL scalp (CLAUDE.md §21 "Session changes 2026-07-09"), which
   discarded the only OOS validation QuantG has ever produced. This reverts that.

HONEST CAVEAT — both shootout rows grade FRAGILE, not CANDIDATE_EDGE (in-sample
positive, out-of-sample negative: -Rs2,467 and -Rs1,530). That means "worth
forward-papering", NEVER "proven". These are seeded PAPER and PAUSED.
CORE_ENGINE_LIVE_ENABLED stays false; live remains the founder ladder.

Run in-container:
    docker exec quantg-backend python /app/scripts/seed_hold_to_expiry_sleeve_08_03.py
    docker exec quantg-backend python /app/scripts/seed_hold_to_expiry_sleeve_08_03.py --apply
    docker exec quantg-backend python /app/scripts/seed_hold_to_expiry_sleeve_08_03.py --apply --arm
"""
import argparse
import copy
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /app

import pymongo  # noqa: E402

try:
    from core.strategy_registry import validate_strategy_doc  # noqa: E402
except Exception:  # pragma: no cover - registry is optional for a dry run
    validate_strategy_doc = None

HTE_NAME = "HTE NIFTY Defined-Risk Put Spread"
HTE_ID = "hte-nifty-defined-risk-putspread"
QG_O1_NAME_RE = "QG-O1"

# Entry: a slow, selective seller. The signal is deliberately unclever — the
# shootout result came from a structure-neutral "every N days" entry, so claiming
# a smart signal here would be claiming evidence that does not exist. What is
# being tested is the HOLD, not the trigger.
#
# One entry per day, morning only, and the anti-pyramiding guard plus the
# one-position-per-(strategy,underlying) rule keep exactly one spread alive; it
# re-arms when the previous one settles at expiry.
HTE_CODE = '''def run(data):
    if len(data) < 20:
        return []
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    # Morning entry only. A held-to-expiry position does not need a precise entry
    # minute -- it needs days -- so the window is wide and the time of day is not
    # a signal.
    if clock and (clock < '09:30' or clock > '12:00'):
        return []
    return [{
        'date': d['date'], 'action': 'SELL', 'direction': 'PE',
        'setup_type': 'hold_to_expiry_defined_risk_credit_spread', 'confidence': 60.0,
        'entry_reason': 'held-to-expiry OTM put credit spread - theta gets its full life',
        'regime_required': 'any', 'option_selection_preference': 'OTM',
        'res2_gate': True,
        'signal_version': 'hte-v1', 'strategy_logic_version': 'hte-2026-08-03'
    }]
'''

# Geometry = the shootout's winning row. Width 10 and ~3% OTM is also the exact
# §15.5 QG-O1 geometry that produced QuantG's only CANDIDATE_EDGE verdict.
HTE_OPTIONS = {
    "enabled": True, "underlying": "NIFTY", "structure": "credit_spread",
    "short_otm_pct": 0.03,          # ~3% OTM short strike
    "spread_width": 10, "wing_width": 10, "spread_width_strikes": 10,
    "exit_mode": "expiry",          # <-- THE POINT. Rides overnight to settlement.
    "target_dte_days": 8,           # DECORATIVE (§21.5) - selection takes the nearest
                                    # chain expiry. min/max DTE below is the real gate.
    "min_dte_days": 5, "max_dte_days": 15,
    "lots": 1, "expiry_offset": 0, "product": "NRML", "candle_interval": "5minute",
    "specialist_role": "slow_premium_hte",
    "owned_regimes": ["RANGE", "INSIDE_QUIET"],
    # Sized from the FIXED max loss, not from a stop distance. width 10 x 50 pts x
    # lot 65 caps the loss per lot; this budget keeps it to ~1 lot in paper.
    "required_capital": 8000.0,
    # NOTE: credit_tp_frac / credit_sl_mult are deliberately ABSENT, not zero.
    # strategy_registry treats "present but 0" as an out-of-range intraday target
    # AND as ambiguous exit intent; absent + exit_mode="expiry" + time_exit 0 is
    # how it encodes "this one genuinely rides to settlement". The whole thesis is
    # that theta gets its full remaining life, so a TP would re-introduce exactly
    # the early exit the shootout says loses money.
}
HTE_RISK = {
    "exit_mode": "hold_to_expiry",
    "max_hold_days": 20,
    "time_exit_minutes": 0,         # no clock exit - 86% of the live book's exits
                                    # were clock-driven and that pool is the loss
    "daily_loss_limit": 0,          # defined risk; the wing bounds the loss and the
                                    # killswitch exemption (LOSS_KILLSWITCH_SKIP_
                                    # HOLD_TO_EXPIRY) relies on this being intentional
}


def _seed_hte(db, template, args) -> int:
    doc = copy.deepcopy(template)
    doc.pop("_id", None)
    doc["id"] = HTE_ID
    doc["name"] = HTE_NAME
    doc["description"] = (
        "Held-to-expiry ~3% OTM NIFTY put credit spread, width 10, entered 5-15 DTE. "
        "No clock exit and no percentage stop: the bought wing is the stop and max loss "
        "is known at entry. Seeded from a 1,869-day structure shootout in which every "
        "early-exit structure lost and both held-to-expiry structures beat breakeven. "
        "FRAGILE, not proven - forward-paper evidence only."
    )
    doc["python_code"] = HTE_CODE
    doc["required_capital"] = HTE_OPTIONS["required_capital"]
    armed = bool(args.arm)
    doc["status"] = "paused"
    doc["schedule_paused"] = armed          # True -> the 09:15 IST wake picks it up
    doc["manual_paused"] = not armed
    doc["archived_at"] = None
    doc["created_at"] = datetime.now(timezone.utc).isoformat()
    doc["founder_forced_live"] = True
    doc["geometry_changed_at"] = datetime.now(timezone.utc).isoformat()
    doc["geometry_change_note"] = "seeded 2026-08-03 from the held-to-expiry shootout"

    vc = doc.setdefault("visual_config", {})
    vc["symbol"] = "NIFTY"
    vc["exchange"] = "NFO"
    vc["options"] = dict(HTE_OPTIONS)
    vc["risk"] = dict(HTE_RISK)

    if validate_strategy_doc is not None:
        v = validate_strategy_doc(doc)
        print(f"  coherence: ok={v.ok} errors={v.errors} warnings={v.warnings}")
        if not v.ok:
            print("  ABORT — coherence errors.")
            return 1

    existing = db.strategies.find_one({"$or": [{"name": HTE_NAME}, {"id": HTE_ID}]})
    print(f"  [{'APPLY' if args.apply else 'DRY-RUN'}] {'UPDATE' if existing else 'SEED'} "
          f"{HTE_NAME}  status=paused armed={armed} exit_mode=expiry width=10 otm=3%")
    if args.apply:
        if existing:
            db.strategies.update_one({"_id": existing["_id"]}, {"$set": {
                "python_code": doc["python_code"], "visual_config": doc["visual_config"],
                "required_capital": doc["required_capital"], "description": doc["description"],
                "status": doc["status"], "schedule_paused": doc["schedule_paused"],
                "manual_paused": doc["manual_paused"], "archived_at": None,
                "founder_forced_live": True,
                "geometry_changed_at": doc["geometry_changed_at"],
                "geometry_change_note": doc["geometry_change_note"],
            }})
        else:
            doc.setdefault("_id", str(uuid.uuid4()))
            doc.setdefault("user_id", template.get("user_id"))
            db.strategies.insert_one(doc)
        print("  written.")
    return 0


def _restore_qgo1(db, args) -> int:
    """Revert the 2026-07-09 intraday conversion that discarded QG-O1's §15.5 OOS pass."""
    row = db.strategies.find_one({"name": {"$regex": QG_O1_NAME_RE}})
    if not row:
        print("  QG-O1 not found — skipping restore.")
        return 0
    vc = copy.deepcopy(row.get("visual_config") or {})
    opt = vc.setdefault("options", {})
    risk = vc.setdefault("risk", {})
    before = {"exit_mode": opt.get("exit_mode"), "risk_exit_mode": risk.get("exit_mode"),
              "tp": opt.get("credit_tp_frac"), "sl": opt.get("credit_sl_mult"),
              "time_exit": risk.get("time_exit_minutes")}

    opt["exit_mode"] = "expiry"
    # Remove rather than zero — see the note on HTE_OPTIONS.
    opt.pop("credit_tp_frac", None)
    opt.pop("credit_sl_mult", None)
    risk["exit_mode"] = "hold_to_expiry"
    risk["time_exit_minutes"] = 0
    risk["max_hold_days"] = 20

    after = {"exit_mode": opt.get("exit_mode"), "risk_exit_mode": risk.get("exit_mode"),
             "tp": opt.get("credit_tp_frac"), "sl": opt.get("credit_sl_mult"),
             "time_exit": risk.get("time_exit_minutes")}
    print(f"  [{'APPLY' if args.apply else 'DRY-RUN'}] RESTORE {row.get('name')}")
    print(f"    before: {before}")
    print(f"    after : {after}")
    if validate_strategy_doc is not None:
        _v = validate_strategy_doc(dict(row, visual_config=vc))
        print(f"    coherence: ok={_v.ok} errors={_v.errors} warnings={_v.warnings}")
        if not _v.ok:
            print("    ABORT — coherence errors on the restored QG-O1.")
            return 1
    if args.apply:
        db.strategies.update_one({"_id": row["_id"]}, {"$set": {
            "visual_config": vc,
            "schedule_paused": bool(args.arm),
            "manual_paused": not bool(args.arm),
            "geometry_changed_at": datetime.now(timezone.utc).isoformat(),
            "geometry_change_note": (
                "2026-08-03: restored to held-to-expiry (reverts the 2026-07-09 intraday "
                "conversion that discarded the §15.5 OOS validation)"),
        }})
        print("    written.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--arm", action="store_true", help="arm for the 09:15 IST wake")
    ap.add_argument("--skip-qgo1", action="store_true")
    args = ap.parse_args()

    db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
                             serverSelectionTimeoutMS=5000)[os.environ.get("DB_NAME", "quantg")]

    template = (db.strategies.find_one({"name": {"$regex": "QG-O1"}})
                or db.strategies.find_one({"visual_config.options.structure": "credit_spread"}))
    if not template:
        print("ERROR: no credit_spread strategy to clone schema from.")
        return 1

    print("== 1. held-to-expiry sleeve ==")
    rc = _seed_hte(db, template, args)
    if rc:
        return rc
    if not args.skip_qgo1:
        print("== 2. restore QG-O1 to its validated held-to-expiry form ==")
        rc = _restore_qgo1(db, args)
        if rc:
            return rc
    if not args.apply:
        print("\nre-run with --apply (add --arm to wake at 09:15 IST).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
