#!/usr/bin/env python3
"""DB migration for the 2026-08-04 session defects. Idempotent; dry-run by default.

Code templates do NOT reach live strategy rows — ERP Phase 0 disabled startup
template sync (CLAUDE.md §20.1) — so every config change needs BOTH the code
template and a migration like this one. Run with --apply to write.

Two rows change:

1. `tail-hedge-nifty-farotm-putspread`
   - `expiry_offset: 0` with no DTE window meant "nearest expiry", so on
     2026-08-04 the hedge bought a **0-DTE** put spread. Crash insurance that
     expires in hours is not insurance, and it contradicts the sleeve's own
     `risk.max_hold_days: 8`. Adds min/max DTE 5-15, which `select_expiry`
     (§25.4b) honours — standing down when nothing qualifies rather than
     substituting a tenor the strategy never asked for.
   - `max_trades_day: 3 -> 1`. It used all three that day: +Rs7,176 on the
     morning slide, then two re-entries near the LOW that gave back Rs6,187.
     It is a HELD hedge, not an intraday trader.

2. `idx-nifty-reversal-0001` (IDX NIFTY Mean-Reversion Fade)
   - `required_capital: 20000 -> 8000`. At 20,000 it sized to FIVE lots and put
     Rs15,811 of defined risk on a single 0-DTE debit spread, losing Rs8,077 —
     129% of the whole day's loss — while every other strategy in the book runs
     4,000-13,000 and trades one lot. `spread_builder.MAX_RISK_PER_TRADE_RUPEES`
     now caps this book-wide, but the row's own budget should not be an outlier
     that depends on a global backstop to be safe.

Nothing here creates edge. It removes a tenor mismatch and a sizing outlier.
"""
from __future__ import annotations

import argparse
import os
import sys

import pymongo

CHANGES = [
    {
        "id": "tail-hedge-nifty-farotm-putspread",
        "expect_name": "Tail Hedge NIFTY Far-OTM Put Spread",
        "set": {
            "visual_config.options.min_dte_days": 5,
            "visual_config.options.max_dte_days": 15,
            "visual_config.risk.max_trades_day": 1,
        },
        "note": "0-DTE hedge + 3 intraday re-entries (2026-08-04)",
    },
    {
        "id": "idx-nifty-reversal-0001",
        "expect_name": "IDX NIFTY Mean-Reversion Fade (debit)",
        "set": {
            "visual_config.options.required_capital": 8000.0,
            "visual_config.risk.required_capital": 8000.0,
        },
        "note": "Rs15,811 defined risk on one 0-DTE spread (2026-08-04)",
    },
]


def _get(doc, dotted):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = ap.parse_args()

    db = pymongo.MongoClient(
        os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
        serverSelectionTimeoutMS=5000,
    )[os.environ.get("DB_NAME", "quantg")]

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== fix_session_defects_08_04  [{mode}] ===\n")
    touched = 0
    missing = 0

    for ch in CHANGES:
        doc = db.strategies.find_one({"id": ch["id"]})
        if not doc:
            print(f"  !! {ch['id']}: NOT FOUND — skipping")
            missing += 1
            continue
        print(f"  {doc.get('name')}  ({ch['id']})")
        print(f"     reason: {ch['note']}")
        pending = {}
        for path, new in ch["set"].items():
            old = _get(doc, path)
            same = str(old) == str(new)
            print(f"       {path}: {old!r} -> {new!r}{'   (already set)' if same else ''}")
            if not same:
                pending[path] = new
        if not pending:
            print("     nothing to change\n")
            continue
        touched += 1
        if args.apply:
            db.strategies.update_one({"id": ch["id"]}, {"$set": pending})
            print(f"     WROTE {len(pending)} field(s)\n")
        else:
            print(f"     would write {len(pending)} field(s)\n")

    print(f"rows needing change: {touched}   not found: {missing}")
    if not args.apply and touched:
        print("\nre-run with --apply to write.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
