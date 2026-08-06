"""F3 + F4 — restore seller SL symmetry and give each seller its OWN DTE window.

Dry-run by default; pass --apply to write. Idempotent: re-running after an apply is
a no-op and prints "no change".

WHY (measured 2026-08-06 on the live book, geometry epoch = the 30 Jul re-cut):

F3 — every intraday credit seller runs credit_tp_frac 0.25 / credit_sl_mult 0.60, so
break-even WR = 0.60/(0.60+0.25) = 70.6%. Realized:

    IDX SENSEX VRP Put-Spread   n=33  WR 30%  -Rs9,966   <- crosses the n>=30 bar
    RAE SENSEX Range Seller     n=20  WR 40%  -Rs4,970
    RAE NIFTY Range Seller      n=13  WR 38%  -Rs2,243

The 30 Jul retune halved TP (0.50 -> 0.25, correctly: only 12% of trades ever reached
0.50) but cut SL by only a third (0.90 -> 0.60), so it moved break-even 64% -> 71% —
one side of a ratio changed. Setting SL 0.45 restores break-even to 64.3% at the
reachable TP. This is MITIGATION, NOT A CURE: 64% is still above the realized 30-43%,
and the sleeve owes a judge run per CLAUDE.md §13.5. Founder chose this over pausing.

F4 — the founder-directed alternative to blanket env gates. Rather than flipping
SELLER_DTE_POLICY_ENABLED globally (which vetoes the whole book indiscriminately),
each seller gets its own DTE window, honoured by core.dte_policy.select_expiry via
the runner's resolver. DTE at entry is the strongest separator in the 259-trade study:

    DTE 0    n=56  WR 80%  +Rs123      <- the only strongly positive bucket
    DTE 1-2  n=49  WR 63%   -Rs75
    DTE 3-5  n=35  WR 23%  -Rs121
    DTE 5-7  n=31  WR 35%  -Rs380
    DTE 7+   n=81  WR 31%  -Rs235      <- -Rs19,015 total

Window [0,2] keeps the positive DTE-0 bucket plus the mildly negative 1-2, and hard
-blocks DTE 3+ where the real damage is. Verified live: NIFTY sellers were entering at
DTE 5-6 (expiry 08-11 from a Thursday) and stopping out. Coverage stays whole across
the week because the indices expire on different days:

    NIFTY  weekly Tuesday  -> window [0,2] reaches Mon(1), Tue(0)
    SENSEX weekly Thursday -> window [0,2] reaches Tue(2), Wed(1), Thu(0)

select_expiry FAILS CLOSED — nothing inside the window means stand down with a reason,
never substitute a tenor the strategy did not ask for. min_dte 0 survives the config
reader (`_v not in (None, "")`, so 0 is preserved, not treated as unset) — checked.

NOT TOUCHED, deliberately:
  * hold-to-expiry sleeves (exit_mode="expiry") — their window is already [5,15] and
    the DTE stand-down must not apply to them (§25.4).
  * the server.py code templates. They read credit_tp_frac 0.45 / credit_sl_mult 0.9 —
    already a THIRD value, drifted since the 30 Jul DB-only retune. Startup template
    sync is disabled (§20.1) so they do not reach live rows; the DB is authoritative.
    Flagged rather than silently "fixed" so a future reseed is a conscious decision.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

NEW_SL_MULT = 0.45
NEW_MIN_DTE = 0
NEW_MAX_DTE = 2
NOTE = ("08-06: SL 0.60->0.45 restores break-even 70.6%->64.3% (the 30 Jul retune "
        "changed only one side of the ratio); per-strategy DTE window [0,2] blocks "
        "the DTE 3+ bucket (-Rs35k/147 trades) without a blanket book-wide gate.")


def _is_intraday_credit_seller(s: dict) -> bool:
    vc = s.get("visual_config") or {}
    o = vc.get("options") or {}
    structure = str(o.get("structure") or s.get("structure") or "")
    if structure != "credit_spread":
        return False
    # A hold-to-expiry sleeve is a different animal: no intraday TP/SL, and the DTE
    # stand-down must never apply to it.
    if str(o.get("exit_mode") or "") == "expiry":
        return False
    if str((vc.get("risk") or {}).get("exit_mode") or "") == "hold_to_expiry":
        return False
    return o.get("credit_tp_frac") is not None and o.get("credit_sl_mult") is not None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    client = AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))
    db = client[os.environ.get("DB_NAME", "quantg")]
    now = datetime.now(timezone.utc).isoformat()

    rows = await db.strategies.find(
        {"status": {"$in": ["live", "active", "paused"]}}).to_list(500)
    targets = [s for s in rows if _is_intraday_credit_seller(s)]

    print(f"{'APPLY' if args.apply else 'DRY RUN'} — {len(targets)} intraday credit seller(s)\n")
    changed = 0
    for s in sorted(targets, key=lambda r: r.get("name", "")):
        o = (s.get("visual_config") or {}).get("options") or {}
        tp = o.get("credit_tp_frac")
        sl_before = o.get("credit_sl_mult")
        lo_before, hi_before = o.get("min_dte_days"), o.get("max_dte_days")

        sets = {}
        if sl_before != NEW_SL_MULT:
            sets["visual_config.options.credit_sl_mult"] = NEW_SL_MULT
        if lo_before != NEW_MIN_DTE:
            sets["visual_config.options.min_dte_days"] = NEW_MIN_DTE
        if hi_before != NEW_MAX_DTE:
            sets["visual_config.options.max_dte_days"] = NEW_MAX_DTE

        def _be(_tp, _sl):
            try:
                return f"{100.0 * float(_sl) / (float(_sl) + float(_tp)):.1f}%"
            except (TypeError, ValueError, ZeroDivisionError):
                return "n/a"

        print(f"  {s.get('name')}  [{s.get('id')}]  status={s.get('status')}")
        print(f"      sl_mult   {sl_before} -> {NEW_SL_MULT}"
              f"   break-even {_be(tp, sl_before)} -> {_be(tp, NEW_SL_MULT)}  (tp={tp})")
        print(f"      dte_window [{lo_before},{hi_before}] -> [{NEW_MIN_DTE},{NEW_MAX_DTE}]")
        if not sets:
            print("      => no change (already migrated)\n")
            continue
        changed += 1
        if args.apply:
            # geometry_changed_at scopes realized-evidence probes to the current shape
            # (§21.5) so a re-cut cannot be graded on trades from the previous geometry.
            sets["visual_config.options.geometry_changed_at"] = now
            sets["visual_config.options.geometry_change_note"] = NOTE
            sets["updated_at"] = now
            res = await db.strategies.update_one({"id": s.get("id")}, {"$set": sets})
            print(f"      => WROTE {len(sets)} field(s), matched={res.matched_count}\n")
        else:
            print(f"      => would write {len(sets)} field(s)\n")

    print(f"{'wrote' if args.apply else 'would write'}: {changed} strategy(ies)")
    if not args.apply:
        print("\nre-run with --apply to write.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
