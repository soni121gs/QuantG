"""Migration (ERP P5-M6) — cost-floor siblings left unfixed on 2026-07-22.

Same measured lever as QG-O4 (P5-M6, `fix_qgo4_costfloor_and_epoch_07_22.py`):
raise `credit_tp_frac` 0.50 -> 0.60 so bankable profit (tp x credit x lot) clears the
3x cost floor, which also LOWERS break-even WR (sl/(sl+tp)) and leaves max-loss-per-lot
— hence `required_capital` and lot sizing — UNCHANGED (§21.4: never narrow a wing/raise
a target's size lever without re-deriving required_capital; tp does not touch max loss,
so it is safe).

Targets (surfaced after `static.cost_floor` was corrected to measure BANKABLE, not
gross, credit — §21.5):
  * IDX NIFTY VRP Call-Spread (RANGE+rich)   ~787/lot   [founder_forced_live]
  * IDX SENSEX VRP Put-Spread (RANGE+rich)   ~894 < 900 [founder_forced_live]
  * RAE SENSEX Range Seller (RANGE/INSIDE)   ~894 < 900

WARNING - TWO gates before applying on the VPS:
  1. §21.4 — the two IDX rows are `founder_forced_live`. Registry-scoped fixes MISS
     them, and they should not be silently rewritten. This script REPORTS the flag and
     refuses to touch a founder-forced row unless `--include-founder-forced` is passed.
  2. §21.9 (2026-07-30) re-measured round-trip friction at ~1/12 of the 300/lot constant
     these vetoes were computed against (NIFTY ~46, SENSEX ~26/lot). The cost floor may
     no longer BE binding for these rows. Re-run `static.cost_floor` / the judge at the
     corrected friction BEFORE deciding whether this migration is still needed.

Idempotent. Dry-run by default; pass --apply to write. Run in the backend container:
  docker exec quantg-backend python /app/scripts/fix_costfloor_siblings_m6.py
  docker exec quantg-backend python /app/scripts/fix_costfloor_siblings_m6.py --apply --include-founder-forced
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

NEW_TP = 0.60
EPOCH_AT = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
NOTE = "credit_tp_frac 0.50->0.60 to clear the 3x cost floor (P5-M6)"

TARGETS = [
    "IDX NIFTY VRP Call-Spread (RANGE+rich)",
    "IDX SENSEX VRP Put-Spread (RANGE+rich)",
    "RAE SENSEX Range Seller (RANGE/INSIDE)",
]


async def main(apply: bool, include_founder_forced: bool) -> int:
    mongo_url = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
    db = AsyncIOMotorClient(mongo_url)[os.environ.get("DB_NAME", "quantg")]

    changed = skipped = 0
    for name in TARGETS:
        rows = await db.strategies.find({"name": name}).to_list(10)
        if not rows:
            print(f"\n### {name}\n    NOT FOUND in db.strategies")
            continue
        for s in rows:
            vc = s.get("visual_config") or {}
            opts = dict(vc.get("options") or {})
            ff = bool(s.get("founder_forced_live"))
            cur_tp = opts.get("credit_tp_frac")
            print(f"\n### {name}  (id={s.get('id')}, status={s.get('status')}, "
                  f"founder_forced_live={ff})")
            if ff and not include_founder_forced:
                print("    SKIPPED — founder_forced_live; pass --include-founder-forced "
                      "AND confirm with the founder (§21.4)")
                skipped += 1
                continue
            if cur_tp == NEW_TP:
                print(f"    already credit_tp_frac={NEW_TP} — no change")
                continue
            print(f"    credit_tp_frac: {cur_tp!r} -> {NEW_TP!r}  "
                  f"(break-even WR {(_sl(opts))/((_sl(opts))+ (cur_tp or 0.5)):.3f} -> "
                  f"{(_sl(opts))/((_sl(opts))+NEW_TP):.3f})")
            changed += 1
            if apply:
                opts["credit_tp_frac"] = NEW_TP
                await db.strategies.update_one(
                    {"_id": s["_id"]},
                    {"$set": {"visual_config.options": opts,
                              "geometry_changed_at": EPOCH_AT,
                              "geometry_change_note": NOTE}},
                )
                print("    APPLIED")

    print(f"\n{changed} row(s) {'updated' if apply else 'would change'}; {skipped} founder-forced skipped.")
    if not apply and changed:
        print("Re-run with --apply to write.")
    return 0


def _sl(opts: dict) -> float:
    try:
        return float(opts.get("credit_sl_mult") or 0.90)
    except (TypeError, ValueError):
        return 0.90


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--include-founder-forced", action="store_true",
                    help="also touch founder_forced_live rows (§21.4 — confirm with founder)")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.apply, a.include_founder_forced)))
