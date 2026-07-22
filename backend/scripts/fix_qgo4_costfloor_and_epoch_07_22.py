"""Migration 2026-07-22b — QG-O4 cost floor + geometry-change epoch stamps.

1. QG-O4 COST FLOOR (Hermes `static.cost_floor`, HIGH).
   Realized 883/lot against a 900 floor (2.94x friction, needs 3.0x). Measured
   against the live SENSEX chain at 1 DTE (scratch/qgo4_geometry_solve_07_22.py),
   the current w6 / delta 0.30 / tp 0.50 shape banks only 818/lot = 2.73x — today's
   chain is thinner than the 2026-07-21 measurement that set the shape, so this
   is drift, not a one-off.

   Solved across BOTH geometry laws plus break-even WR rather than nudging one
   lever. Chosen: raise credit_tp_frac 0.50 -> 0.60, everything else unchanged.
       bankable  982/lot = 3.27x friction   (was 818 = 2.73x)   -> clears with margin
       break-even WR 0.90/(0.90+0.60) = 0.60 (was 0.643)        -> IMPROVES
       reachability at 0/1/2 DTE = 4.4 / 1.33 / 0.67            -> clears
       max_loss per lot UNCHANGED (10,363) so required_capital 10500 still buys
       exactly 1 lot -- no silent size-up (§21.4).
   The alternatives were rejected: tp 0.55 lands at exactly 3.00x (no margin
   against further chain drift); w7 costs 18% more capital on the book's most
   marginal strategy; delta 0.35 moves the short leg closer to the money and
   trades win rate for a beWR that barely improves.

   Consequence: at 3 DTE (Monday) the reachability guard now vetoes this shape,
   so QG-O4 trades Tue-Thu. Stand-down is a strategy (§18.4).

2. GEOMETRY-CHANGE EPOCH (`strategy.persistent_live_loss`, MEDIUM x2).
   QG-O1 (-7,722 / 18 trades) and QG-O11 (-7,720 / 35 trades) are net-negative,
   but every one of those trades ran the PRE-2026-07-22 machine: an immature-RANGE
   regime label that let them sell into trends, and a take-profit that decay could
   not reach inside the hold. Both causes are fixed. The trailing number is now
   measuring a strategy that no longer exists.

   This does NOT silence the finding — it still fires, at the same severity, and
   still shows the pre-change loss. It stamps `geometry_changed_at` so the probe
   can SPLIT the sample and say "0 of 35 trades have run the current geometry"
   instead of presenting a stale number as a verdict on the current one. The
   finding stays open until the post-change sample is both large enough AND
   positive. Grading resets are how losing strategies get laundered, so the split
   is additive evidence only.

Idempotent. Dry-run by default; pass --apply to write.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

EPOCH_AT = "2026-07-22T00:00:00+00:00"
EPOCH_NOTE = ("regime-confidence fix + theta-reachability guard "
              "(commit d34b7ce) — all earlier trades ran the pre-fix machine")

PLAN = {
    # Cost-floor re-cut AND an epoch stamp (its geometry changed too).
    "QG-O4 SENSEX Call Spread Range Pilot": {
        "options": {"credit_tp_frac": 0.60},
        "epoch_note": "credit_tp_frac 0.50->0.60 to clear the 3x cost floor (2.73x -> 3.27x)",
    },
    # Epoch stamp only — their geometry was corrected in the 07-22 commit/migration.
    "QG-O1 NIFTY Put Spread Theta Core": {
        "options": {},
        "epoch_note": EPOCH_NOTE + "; also restored to hold-to-expiry",
    },
    "QG-O11 NIFTY Regime Seller Credit Scalp": {
        "options": {},
        "epoch_note": EPOCH_NOTE + "; now near-expiry-only via the reachability guard",
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
            diff = {}
            for k, v in patch["options"].items():
                if opts.get(k) != v:
                    diff[k] = (opts.get(k), v)
                    opts[k] = v
            if s.get("geometry_changed_at") != EPOCH_AT:
                diff["geometry_changed_at"] = (s.get("geometry_changed_at"), EPOCH_AT)
            if s.get("geometry_change_note") != patch["epoch_note"]:
                diff["geometry_change_note"] = ("...", patch["epoch_note"][:60] + "...")

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
                    {"$set": {"visual_config.options": opts,
                              "geometry_changed_at": EPOCH_AT,
                              "geometry_change_note": patch["epoch_note"]}},
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
