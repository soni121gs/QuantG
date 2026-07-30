"""Re-cut every live credit seller's take-profit to a REACHABLE target.

Measured over 92 closed spreads with peak P&L recorded, as a fraction of maximum
credit (net_credit x qty):

    tp 0.15  reached by 55% of trades
    tp 0.25  reached by 33%
    tp 0.35  reached by 20%
    tp 0.50  reached by 12%      <- the book was set at 0.45-0.50
    median peak across ALL trades: 0.174

So the configured target was one the market paid on roughly one trade in eight.
Worse, `spread-time-exit` (n=35, 38% of the sample) had a median peak of 0.3% of
credit — those trades never went green at all, so no target would have helped them.

Founder-approved 2026-07-30: tp_frac -> 0.25. Paired with the friction
re-measurement (slippage 3% -> 0.5%/leg) so the smaller target still clears the
cost floor with room: SENSEX credit 50 x lot 20 x 0.25 = Rs250 bankable against
~Rs25 real friction (10x), where before it was Rs450 bankable against a modelled
Rs300 (1.5x).

Startup template sync is disabled (ERP Phase 0, §20.1), so a code-template change
does NOT reach the live rows — this migration is the other half. Idempotent;
dry-run by default; prints a before/after diff.

    python scripts/retune_tp_frac_07_30.py            # dry run
    python scripts/retune_tp_frac_07_30.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

NEW_TP_FRAC = float(os.environ.get("RETUNE_TP_FRAC", "0.25"))
NOTE = ("2026-07-30: tp_frac -> {:.2f}. Only 12% of 92 measured trades ever reached "
        "0.50 of credit (median peak 0.174); 0.25 is reached by 33%. Paired with the "
        "friction re-measurement (slippage 3% -> 0.5%/leg).").format(NEW_TP_FRAC)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--tp", type=float, default=NEW_TP_FRAC)
    args = ap.parse_args()

    db = AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017")).quantg
    rows = await db.strategies.find(
        {"visual_config.options.structure": "credit_spread"},
        {"_id": 0, "id": 1, "name": 1, "status": 1, "visual_config": 1},
    ).to_list(500)

    print(f"credit-spread strategies found: {len(rows)}   target tp_frac={args.tp}")
    print(f"{'status':8} {'old_tp':>7} {'new_tp':>7}  name")
    changed = 0
    for r in rows:
        opts = ((r.get("visual_config") or {}).get("options") or {})
        old = opts.get("credit_tp_frac")
        if old is not None and abs(float(old) - args.tp) < 1e-9:
            print(f"{r.get('status',''):8} {float(old):7.2f} {'  same':>7}  {r.get('name')}")
            continue
        print(f"{r.get('status',''):8} {('  none' if old is None else f'{float(old):7.2f}')} "
              f"{args.tp:7.2f}  {r.get('name')}")
        changed += 1
        if args.apply:
            await db.strategies.update_one(
                {"id": r["id"]},
                {"$set": {
                    "visual_config.options.credit_tp_frac": args.tp,
                    # Epoch marker so realized-evidence probes scope to the new shape
                    # and a losing strategy cannot be laundered by a grading reset
                    # (§21.5).
                    "geometry_changed_at": datetime.now(timezone.utc).isoformat(),
                    "geometry_change_note": NOTE,
                }},
            )

    print(f"\n{'APPLIED' if args.apply else 'DRY RUN'} — {changed} strategy row(s) "
          f"{'updated' if args.apply else 'would change'}")
    if not args.apply and changed:
        print("re-run with --apply to write")


if __name__ == "__main__":
    asyncio.run(main())
