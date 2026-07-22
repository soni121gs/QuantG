"""Migration 2026-07-22 — close the two defects that produced the −₹1,967 session.

ERP Phase 0 disabled startup template sync (CLAUDE.md §20.1), so a code edit never
reaches a live strategy row. Everything the running book needs has to arrive here.

Two changes, both per CLAUDE.md law:

1. SPECIALIST TAGS (Hermes `static.specialist_tag_consistency`, 3 open findings).
   QG-O1 / QG-O4 / QG-O11 carry no `specialist_role`, so the RAE router defaults
   them to `range_seller` silently. The default happens to be right, but an
   implicit tag cannot be audited, cannot stand down, and cannot be graded by
   regime — the whole point of §18. Tag them explicitly.

2. THETA REACHABILITY (§21.2, Hermes `exec.exit_reason_mix`, critical).
   `target_dte_days` is decorative — no selection code reads it — so these
   strategies take whatever expiry the chain offers. On a Wednesday the nearest
   NIFTY weekly is 6 days out, and a 300-minute hold can decay ~13% of the credit
   against a 45% take-profit (ratio 0.30). Measured 2026-07-22: 5 of 5 spreads
   closed on the clock, none on price.

   The build-time guard (core/spread_builder, this same commit) now refuses those
   geometries, which alone would leave QG-O1 unable to trade Wed–Fri. So restore
   the hold each strategy needs to make its own target reachable:

     QG-O1  → hold to expiry. This is what it was BEFORE 2026-07-09 and the only
              form of it that ever passed OOS (§15.5). Held to expiry theta gets
              its full remaining life, the whole credit is bankable, and the
              reachability law does not bite (the builder exempts exit_mode
              "expiry"). Its RES-2 IV-rich + RANGE gate stays on.
     QG-O11 → stays the intraday 1-min scalp it is designed to be, but its target
              comes down to what 300 minutes of decay can actually deliver on the
              expiries it will meet, and it now only fires when the reachability
              guard passes — i.e. it works near expiry (Mon/Tue for NIFTY) and
              stands down the rest of the week. Stand-down is a strategy (§18.4).
     QG-O4  → SENSEX expires Thursday, so its realized DTE is already 1–2 and its
              ratio is fine. Tag only; geometry untouched.

Neither strategy is paused. Both keep trading the conditions their structure
actually works in.

Idempotent. Dry-run by default; pass --apply to write.

  docker exec quantg-backend python /app/scripts/fix_seller_regime_and_reachability_07_22.py
  docker exec quantg-backend python /app/scripts/fix_seller_regime_and_reachability_07_22.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

SELLER_TAGS = {"specialist_role": "range_seller",
               "owned_regimes": ["RANGE", "INSIDE_QUIET"]}

# name-match → {options patch, risk patch}
PLAN = {
    "QG-O1 NIFTY Put Spread Theta Core": {
        "options": {**SELLER_TAGS, "exit_mode": "expiry"},
        # Hold to expiry: no intraday clock, and let it re-enter across the week.
        "risk": {"time_exit_minutes": 0, "exit_mode": "signal_or_tp_sl_trailing"},
    },
    "QG-O4 SENSEX Call Spread Range Pilot": {
        "options": {**SELLER_TAGS},
        "risk": {},
    },
    "QG-O11 NIFTY Regime Seller Credit Scalp": {
        "options": {**SELLER_TAGS, "credit_tp_frac": 0.30},
        "risk": {},
    },
}


async def main(apply: bool) -> int:
    mongo_url = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
    db = AsyncIOMotorClient(mongo_url)[os.environ.get("DB_NAME", "quantg")]

    changed = 0
    for name, patch in PLAN.items():
        async for s in db.strategies.find({"name": name}):
            vc = s.get("visual_config") or {}
            opts = dict(vc.get("options") or {})
            risk = dict(vc.get("risk") or {})

            before = {k: opts.get(k) for k in patch["options"]}
            before.update({f"risk.{k}": risk.get(k) for k in patch["risk"]})
            opts.update(patch["options"])
            risk.update(patch["risk"])
            after = {k: opts.get(k) for k in patch["options"]}
            after.update({f"risk.{k}": risk.get(k) for k in patch["risk"]})

            diff = {k: (before[k], after[k]) for k in after if before.get(k) != after[k]}
            print(f"\n### {name}  (id={s.get('id')}, status={s.get('status')})")
            if not diff:
                print("    already current — no change")
                continue
            for k, (b, a) in diff.items():
                print(f"    {k}: {b!r} -> {a!r}")
            changed += 1
            if apply:
                await db.strategies.update_one(
                    {"_id": s["_id"]},
                    {"$set": {"visual_config.options": opts, "visual_config.risk": risk}},
                )
                print("    APPLIED")

    print(f"\n{changed} strategy row(s) {'updated' if apply else 'would change'}.")
    if not apply and changed:
        print("Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.apply)))
