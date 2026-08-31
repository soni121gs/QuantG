#!/usr/bin/env python3
"""Re-cut weak strategies into safer paper-forward experiments.

Dry-run by default; pass --apply to write. This is a DB migration because ERP
template sync is disabled and live paper rows trade from Mongo config.

Evidence used on 2026-08-31:
- IDX NIFTY Mean-Reversion Fade lost Rs8,077 on one oversized 0-DTE debit spread;
  its top/risk capital still said 20k while options said 8k.
- SENSEX credit sellers lost across DTE 1+ but DTE 0 was the least bad / positive
  bucket; they also overtraded with max_trades_day=25.
- RAE NIFTY Range Seller was near breakeven excluding DTE 6+, but repeated
  no-progress/SL exits dominated.
- Tail Hedge made money only when active exits fired; expiry-settlement exits
  were all losers.

These edits do not claim edge. They make each strategy a smaller, more coherent
paper experiment that still owes OOS/replay and forward-paper evidence.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from typing import Any, Dict

import pymongo


NOTE = "08-31 reimagined weak-strategy repair: smaller risk, tighter DTE ownership, fewer repeat entries; owes OOS/replay."


CHANGES: Dict[str, Dict[str, Any]] = {
    "idx-nifty-reversal-0001": {
        "why": "Fix incoherent 20k/8k capital and make reversal a one-lot tactical debit experiment.",
        "set": {
            "required_capital": 4000.0,
            "visual_config.options.required_capital": 4000.0,
            "visual_config.risk.required_capital": 4000.0,
            "visual_config.options.min_dte_days": 1,
            "visual_config.options.max_dte_days": 2,
            "visual_config.risk.max_trades_day": 1,
            "visual_config.risk.cooldown_minutes": 45,
            "visual_config.risk.time_exit_minutes": 45,
            "visual_config.risk.target_pct": 60.0,
            "visual_config.risk.stoploss_pct": 30.0,
        },
    },
    "tail-hedge-nifty-farotm-putspread": {
        "why": "Stop treating hedge as an expiry lottery; keep one small tactical hedge with faster risk-off.",
        "set": {
            "visual_config.options.min_dte_days": 5,
            "visual_config.options.max_dte_days": 15,
            "visual_config.risk.max_trades_day": 1,
            "visual_config.risk.cooldown_minutes": 60,
            "visual_config.risk.time_exit_minutes": 90,
            "visual_config.risk.target_pct": 60.0,
            "visual_config.risk.stoploss_pct": 35.0,
        },
    },
    "rae-range-seller-nifty": {
        "why": "Keep the fixable NIFTY range seller but cut repeat-entry churn and improve payoff symmetry.",
        "set": {
            "visual_config.options.min_dte_days": 0,
            "visual_config.options.max_dte_days": 5,
            "visual_config.options.credit_tp_frac": 0.30,
            "visual_config.options.credit_sl_mult": 0.40,
            "visual_config.risk.max_trades_day": 3,
            "visual_config.risk.cooldown_minutes": 20,
            "visual_config.risk.time_exit_minutes": 240,
        },
    },
    "rae-range-seller-sensex": {
        "why": "SENSEX range seller only gets a same-day pilot; DTE 1+ has been negative.",
        "set": {
            "visual_config.options.min_dte_days": 0,
            "visual_config.options.max_dte_days": 0,
            "visual_config.options.credit_tp_frac": 0.30,
            "visual_config.options.credit_sl_mult": 0.40,
            "visual_config.risk.max_trades_day": 2,
            "visual_config.risk.cooldown_minutes": 25,
            "visual_config.risk.time_exit_minutes": 210,
        },
    },
    "idx-sensex-putspread-0001": {
        "why": "SENSEX VRP put seller only gets a same-day pilot; DTE 1+ and repeated SL/no-progress drove loss.",
        "set": {
            "visual_config.options.min_dte_days": 0,
            "visual_config.options.max_dte_days": 0,
            "visual_config.options.credit_tp_frac": 0.30,
            "visual_config.options.credit_sl_mult": 0.40,
            "visual_config.risk.max_trades_day": 2,
            "visual_config.risk.cooldown_minutes": 25,
            "visual_config.risk.time_exit_minutes": 210,
        },
    },
    "idx-nifty-callspread-0001": {
        "why": "NIFTY call seller remains only a low-frequency pilot; reduce churn and improve payoff symmetry.",
        "set": {
            "visual_config.options.min_dte_days": 0,
            "visual_config.options.max_dte_days": 2,
            "visual_config.options.credit_tp_frac": 0.30,
            "visual_config.options.credit_sl_mult": 0.40,
            "visual_config.risk.max_trades_day": 3,
            "visual_config.risk.cooldown_minutes": 20,
            "visual_config.risk.time_exit_minutes": 240,
        },
    },
}


def _get(doc: Dict[str, Any], dotted: str) -> Any:
    cur: Any = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes")
    args = ap.parse_args()

    db = pymongo.MongoClient(
        os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
        serverSelectionTimeoutMS=5000,
    )[os.environ.get("DB_NAME", "quantg")]
    now = datetime.now(timezone.utc).isoformat()
    print(("APPLY" if args.apply else "DRY RUN") + " weak-strategy repair\n")

    changed = 0
    for sid, spec in CHANGES.items():
        doc = db.strategies.find_one({"id": sid})
        if not doc:
            print(f"{sid}: not found")
            continue
        sets = {}
        print(f"{doc.get('name')} [{sid}]")
        print(f"  why: {spec['why']}")
        for path, new in spec["set"].items():
            old = _get(doc, path)
            same = str(old) == str(new)
            print(f"  {path}: {old!r} -> {new!r}{' (already)' if same else ''}")
            if not same:
                sets[path] = new
        if sets:
            sets["visual_config.options.geometry_changed_at"] = now
            sets["visual_config.options.geometry_change_note"] = NOTE
            sets["updated_at"] = now
            changed += 1
            if args.apply:
                res = db.strategies.update_one({"id": sid}, {"$set": sets})
                print(f"  wrote {len(sets)} fields matched={res.matched_count}\n")
            else:
                print(f"  would write {len(sets)} fields\n")
        else:
            print("  no change\n")
    print(f"{'wrote' if args.apply else 'would write'} {changed} strategy rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
