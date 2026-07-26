"""Re-cut the three SENSEX credit sellers so they satisfy BOTH §21 laws (2026-07-26).

THE PROBLEM
-----------
SENSEX expires THURSDAY (verified from our own fills: 07-09, 07-16, 07-23), so on a
Monday the nearest expiry is 3 DTE. At width 6 / 300-min hold the three SENSEX sellers
could not satisfy both geometry laws at 3 DTE *at any take-profit*:

    tp 0.50 -> reachability 0.53 (< 0.55)  FAIL   |  cost floor 2.77x (< 3.0)  FAIL
    tp 0.45 -> reachability 0.59            pass  |  cost floor 2.49x          FAIL
    tp 0.40 -> reachability 0.67            pass  |  cost floor 2.22x          FAIL
    tp 0.60 -> reachability NEVER buildable FAIL  |  cost floor 3.27x          pass

The laws pull in opposite directions (CLAUDE.md §21.2), and at width 6 the feasible
set is EMPTY. This is how QG-O4 got stuck: `fix_qgo4_costfloor_and_epoch_07_22.py`
raised its tp 0.50->0.60 to clear the cost floor, which made reachability permanently
unsatisfiable at 3 DTE. Trading one law against the other cannot converge.

THE FIX
-------
Widen the wing. A wider wing raises the credit (helps the cost floor) and does not
touch reachability, so tp can return to 0.50 and reachability is fixed with the hold
instead. Verified against REAL 3-DTE SENSEX fills at width 800 (2026-07-20, exp 07-23):

    77400/76600  credit 169.21  cost multiple 3.67  PASS
    77200/76400  credit 186.53  cost multiple 3.52  PASS
    77100/76300  credit 187.02  cost multiple 3.29  PASS
    reachability(tp 0.50, dte 3, hold 330) = 0.587  PASS

CAPITAL IS LOAD-BEARING (§21.4)
-------------------------------
`lots_for_risk` floor-divides the budget by per-lot max loss. Widening 6->8 raises
per-lot max loss from ~Rs9,122 to ~Rs12,600, so the existing Rs10,500 cap would yield
ZERO lots — a silent stand-down that looks identical to a veto. The cap moves to
Rs13,000 (1 lot; 2 lots would need ~Rs25,200).

WHAT THIS DOES NOT DO
---------------------
It does not create edge. It makes the structure fundable and internally coherent so it
can be judged at all. Every re-cut shape is UNVALIDATED and owes a judge run plus
forward-paper per §13.5. The Monday (3-DTE) entry window is 09:45-10:20 — after that
the 15:30 square-off caps the hold below what reachability needs. On Wed/Thu (1/0 DTE)
these build all session as before.

Dry-run by default. Idempotent: re-running rewrites the same values.

    docker exec quantg-backend python /app/scripts/regeometry_sensex_sellers_07_26.py
    docker exec quantg-backend python /app/scripts/regeometry_sensex_sellers_07_26.py --apply
"""
import argparse
import asyncio
import os
import sys
from typing import Any, Dict

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

# Commit time of this re-cut (UTC). A fixed constant, not now(), so re-running is
# idempotent and the persistent-loss epoch split stays stable.
EPOCH_AT = "2026-07-26T14:30:00+00:00"
EPOCH_NOTE = (
    "SENSEX width 6->8 + tp 0.50 + time_exit 330 so BOTH §21 laws hold at 3 DTE "
    "(width 6 had an empty feasible set); required_capital 10500->13000 because the "
    "wider wing raises per-lot max loss and the old cap would size to 0 lots"
)

_OPTIONS_PATCH: Dict[str, Any] = {
    "spread_width": 8,
    "wing_width": 8,
    "credit_tp_frac": 0.50,
    "credit_sl_mult": 0.90,
    "short_delta": 0.30,
    "required_capital": 13000.0,
}
_RISK_PATCH: Dict[str, Any] = {
    "time_exit_minutes": 330,
    "required_capital": 13000.0,
}

PLAN: Dict[str, Dict[str, Any]] = {
    name: {"options": dict(_OPTIONS_PATCH), "risk": dict(_RISK_PATCH),
           "required_capital": 13000.0}
    for name in (
        "QG-O4 SENSEX Call Spread Range Pilot",
        "RAE SENSEX Range Seller (RANGE/INSIDE)",
        "IDX SENSEX VRP Put-Spread (RANGE+rich)",
    )
}


def _diff(before: Dict[str, Any], patch: Dict[str, Any], label: str) -> list:
    out = []
    for key, new in sorted(patch.items()):
        old = before.get(key)
        if old != new:
            out.append(f"      {label}.{key}: {old!r} -> {new!r}")
    return out


async def main(apply: bool) -> int:
    mongo_url = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
    db = AsyncIOMotorClient(mongo_url)[os.environ.get("DB_NAME", "quantg")]

    print(f"{'APPLY' if apply else 'DRY-RUN'}: SENSEX seller re-cut (width 8 / tp 0.50 / hold 330)\n")
    touched = 0
    for name, plan in PLAN.items():
        doc = await db.strategies.find_one({"name": name})
        if not doc:
            print(f"  !! NOT FOUND: {name}")
            continue
        vc = doc.get("visual_config") or {}
        opts = vc.get("options") or {}
        risk = vc.get("risk") or {}

        lines = _diff(opts, plan["options"], "options")
        lines += _diff(risk, plan["risk"], "risk")
        if doc.get("required_capital") != plan["required_capital"]:
            lines.append(
                f"      required_capital: {doc.get('required_capital')!r} -> {plan['required_capital']!r}")

        print(f"  {name}")
        if not lines:
            print("      (already at target geometry)")
        for line in lines:
            print(line)

        if apply:
            await db.strategies.update_one(
                {"id": doc["id"]},
                {"$set": {
                    **{f"visual_config.options.{k}": v for k, v in plan["options"].items()},
                    **{f"visual_config.risk.{k}": v for k, v in plan["risk"].items()},
                    "required_capital": plan["required_capital"],
                    "geometry_changed_at": EPOCH_AT,
                    "geometry_change_note": EPOCH_NOTE,
                }},
            )
            touched += 1
        print()

    print(f"{'updated' if apply else 'would update'} {touched if apply else len(PLAN)} strategies")
    if not apply:
        print("re-run with --apply to write")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    raise SystemExit(asyncio.run(main(ap.parse_args().apply)))
