"""Apply the 2026-07-21 measured seller-book geometry to the LIVE strategy rows.

ERP Phase 0 disabled startup template sync (CLAUDE.md §20.1), so editing the code
templates does NOT reach `db.strategies`. This script is the DB half of that
change set. It is idempotent and prints a before/after diff.

WHY (all measured on the live Upstox chain 2026-07-21 13:35 IST, not modelled):

  1. COST FLOOR — every seller in the book sold at short_delta 0.12-0.14. Probed
     across 3 underlyings x 3 expiries x 6 widths x 5 deltas, delta 0.12 clears
     the ERP 3x-friction cost floor in ZERO cases. At delta 0.30 the same widths
     clear it at 3.0-6.4x. QG-O11's width-1 clears in zero cases at ANY delta.

  2. THETA REACHABILITY — at a 90-120 minute hold, time decay could supply only
     0.05-0.16 of credit against a 0.50 take-profit target, so 68-91% of the
     target had to arrive as a favourable price move. Across the book that showed
     up as 61 of 71 closed trades exiting on a clock rather than a price trigger.
     A 300-minute hold lets theta supply ~0.80 of the target at ~2 DTE.

  3. SIZING — `lots_for_risk` = budget // (max_loss_per_unit * lot_size). The
     narrower wings cut per-lot risk sharply, so the OLD capital caps would have
     silently sized UP (SENSEX 60000/6188 = 9 lots). Caps are re-derived to ~1 lot.

Usage (inside the backend container):
    python /app/scripts/regeometry_seller_book_07_21.py            # dry run
    python /app/scripts/regeometry_seller_book_07_21.py --apply
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Dict

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

# name -> (options patch, risk patch, required_capital)
PLAN: Dict[str, Dict[str, Any]] = {
    "QG-O1 NIFTY Put Spread Theta Core": {
        "options": {"spread_width": 4, "wing_width": 4, "short_delta": 0.30,
                    "short_otm_pct": 0.008, "credit_tp_frac": 0.45,
                    "credit_sl_mult": 0.90, "target_dte_days": 3,
                    "required_capital": 11000.0},
        "risk": {"time_exit_minutes": 300, "daily_loss_limit": 12000.0,
                 "max_trades_day": 3, "required_capital": 11000.0},
        "required_capital": 11000.0,
    },
    "QG-O4 SENSEX Call Spread Range Pilot": {
        "options": {"spread_width": 4, "wing_width": 4, "short_delta": 0.30,
                    "credit_tp_frac": 0.50, "credit_sl_mult": 0.90,
                    "target_dte_days": 2, "required_capital": 7000.0},
        "risk": {"time_exit_minutes": 300, "daily_loss_limit": 9000.0,
                 "max_trades_day": 3, "required_capital": 7000.0},
        "required_capital": 7000.0,
    },
    "QG-O11 NIFTY Regime Seller Credit Scalp": {
        "options": {"spread_width": 4, "wing_width": 4, "short_delta": 0.30,
                    "short_offset_strikes": None, "credit_tp_frac": 0.45,
                    "credit_sl_mult": 0.90, "target_dte_days": 3,
                    "required_capital": 11000.0},
        "risk": {"time_exit_minutes": 300, "daily_loss_limit": 9000.0,
                 "max_trades_day": 2, "required_capital": 11000.0},
        "required_capital": 11000.0,
    },
    "RAE NIFTY Range Seller (RANGE/INSIDE)": {
        "options": {"spread_width": 4, "wing_width": 4, "short_delta": 0.30,
                    "short_otm_pct": 0.012, "credit_tp_frac": 0.45,
                    "credit_sl_mult": 0.90, "target_dte_days": 3,
                    "required_capital": 11000.0},
        "risk": {"time_exit_minutes": 300, "daily_loss_limit": 9000.0,
                 "required_capital": 11000.0},
        "required_capital": 11000.0,
    },
    "RAE BANKNIFTY Range Seller (RANGE/INSIDE)": {
        "options": {"spread_width": 4, "wing_width": 4, "short_delta": 0.30,
                    "short_otm_pct": 0.012, "credit_tp_frac": 0.45,
                    "credit_sl_mult": 0.90, "target_dte_days": 3,
                    "required_capital": 10500.0},
        "risk": {"time_exit_minutes": 300, "daily_loss_limit": 9000.0,
                 "required_capital": 10500.0},
        "required_capital": 10500.0,
    },
    "RAE SENSEX Range Seller (RANGE/INSIDE)": {
        "options": {"spread_width": 4, "wing_width": 4, "short_delta": 0.30,
                    "short_otm_pct": 0.012, "credit_tp_frac": 0.50,
                    "credit_sl_mult": 0.90, "target_dte_days": 2,
                    "required_capital": 7000.0},
        "risk": {"time_exit_minutes": 300, "daily_loss_limit": 9000.0,
                 "required_capital": 7000.0},
        "required_capital": 7000.0,
    },
    # --- IDX sleeves (founder-created 2026-07-20, founder_forced_live) ----------
    # These live rows were built from the RESEARCH configs in
    # `core/index_alpha_sleeves.py`, which are correctly HELD-TO-EXPIRY
    # (exit_mode="expiry", max_hold_days 8-35). Held to expiry, a 3% OTM /
    # width-8-10 wing is a legitimate shape: theta gets its full life and the
    # whole credit is bankable. But the live rows were seeded with a 120-minute
    # hold and a 0.50 TP while KEEPING the held-to-expiry geometry — the same
    # mismatch the 2026-07-09 QG-O1 change introduced. Verified against the live
    # chain post-deploy: both are vetoed by the cost floor (NIFTY credit 20.10 on
    # width 500 -> ratio 0.040; SENSEX credit 67.40 on width 800 -> ratio 0.084),
    # i.e. they would silently stand down forever. Re-cut to the same intraday
    # shape as the rest of the book. The RESEARCH configs are deliberately NOT
    # changed — the EOD judge should keep grading the held-to-expiry version.
    "IDX NIFTY VRP Call-Spread (RANGE+rich)": {
        "options": {"spread_width": 4, "wing_width": 4, "short_delta": 0.30,
                    "short_otm_pct": 0.012, "credit_tp_frac": 0.45,
                    "credit_sl_mult": 0.90, "target_dte_days": 3,
                    "required_capital": 11000.0},
        "risk": {"time_exit_minutes": 300, "daily_loss_limit": 9000.0,
                 "max_trades_day": 3, "required_capital": 11000.0},
        "required_capital": 11000.0,
    },
    "IDX SENSEX VRP Put-Spread (RANGE+rich)": {
        "options": {"spread_width": 4, "wing_width": 4, "short_delta": 0.30,
                    "short_otm_pct": 0.012, "credit_tp_frac": 0.50,
                    "credit_sl_mult": 0.90, "target_dte_days": 2,
                    "required_capital": 7000.0},
        "risk": {"time_exit_minutes": 300, "daily_loss_limit": 9000.0,
                 "max_trades_day": 3, "required_capital": 7000.0},
        "required_capital": 7000.0,
    },
    # NOTE: the two IDX *debit* sleeves (Mean-Reversion Fade, Long-Gamma) are
    # deliberately untouched. They are BUYERS — they pay a debit rather than
    # collecting credit, so the credit cost floor does not apply, and theta works
    # AGAINST them, so a short 90-minute hold is correct rather than a defect.
}

WATCH_OPT = ("spread_width", "wing_width", "short_delta", "short_offset_strikes",
             "credit_tp_frac", "credit_sl_mult", "required_capital")
WATCH_RISK = ("time_exit_minutes", "daily_loss_limit", "max_trades_day")


async def main() -> None:
    apply = "--apply" in sys.argv
    cli = AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))
    db = cli[os.environ.get("DB_NAME", "quantg")]
    changed = 0
    for name, plan in PLAN.items():
        doc = await db.strategies.find_one({"name": name})
        if not doc:
            print(f"SKIP  {name}: not found")
            continue
        vc = doc.get("visual_config") or {}
        opts = dict(vc.get("options") or {})
        risk = dict(vc.get("risk") or {})
        before = ({k: opts.get(k) for k in WATCH_OPT},
                  {k: risk.get(k) for k in WATCH_RISK},
                  doc.get("required_capital"))
        opts.update(plan["options"])
        risk.update(plan["risk"])
        after = ({k: opts.get(k) for k in WATCH_OPT},
                 {k: risk.get(k) for k in WATCH_RISK},
                 plan["required_capital"])
        if before == after:
            print(f"OK    {name}: already at target geometry")
            continue
        changed += 1
        print(f"\n=== {name}  (status={doc.get('status')}, mode={doc.get('mode')})")
        print(f"  options before: {before[0]}")
        print(f"  options after : {after[0]}")
        print(f"  risk    before: {before[1]}")
        print(f"  risk    after : {after[1]}")
        print(f"  required_capital: {before[2]} -> {after[2]}")
        if apply:
            await db.strategies.update_one(
                {"_id": doc["_id"]},
                {"$set": {"visual_config.options": opts,
                          "visual_config.risk": risk,
                          "required_capital": plan["required_capital"],
                          "geometry_revision": "2026-07-21-measured"}},
            )
    print(f"\n{changed} row(s) {'UPDATED' if apply else 'would change (dry run; pass --apply)'}")


if __name__ == "__main__":
    asyncio.run(main())
