"""Retire the unsalvageable, re-gate the salvageable. Dry-run by default; --apply to write.

Decided from a per-strategy DTE decomposition of every closed trade (2026-08-06), not from
aggregate book evidence. The distinction matters: the book-wide [0,2] window set earlier the
same day is derived from the 259-trade aggregate, and for the two SENSEX sellers below it is
exactly the WRONG gate — their losses live INSIDE DTE 0-2. A strategy is gradeable only
against its own record.

RETIRE — no positive DTE bucket to gate toward, and past the n>=30 evidence bar:

  RAE SENSEX Range Seller      n=30  -Rs6,413  payoff b=0.48  breakeven 68% vs WR 47%
      DTE 0 -694 | DTE 1-2 -4,213 | DTE 3-5 -333 | DTE 6+ -1,173   -> 0 of 4 positive
  IDX SENSEX VRP Put-Spread    n=51  -Rs8,830  payoff b=0.43  breakeven 70% vs WR 45%
      DTE 0 -1,702 | DTE 1-2 -6,735 | DTE 3-5 +232 | DTE 6+ -626  -> 1 of 4, and that
      one is n=9 noise. Its two worst buckets are the two the [0,2] window KEEPS.

  Neither is rescuable by geometry: at the new 0.25/0.45 shape break-even is 64.3% and
  they run 45-47%. Archived, not deleted — P&L history is kept (SS13.6). Open positions
  are left alone; the monitor keys on positions, not strategy status, so they close
  normally on their own exits.

RE-GATE — loss is confined to a bucket a window removes:

  QG-O1 NIFTY Put Spread   DTE 0 +1,577 | 1-2 +816 | 3-5 +847 | 6+ -6,498 (n=2, WR 0%)
      Every bucket but one is positive; DTE 6+ IS the entire -Rs3,258. It is hold-to-expiry
      with NO DTE window at all (the SS30.5 open item), so it takes whatever expiry the chain
      offers. Window [0,5] -> the loss pool becomes unreachable while nothing else changes.
  RAE NIFTY Range Seller   DTE 0 +385 | 1-2 +354 | 3-5 +297 | 6+ -1,736
      Same shape: DTE 6+ is the whole -Rs700. Widen today's [0,2] to [0,5] — its own DTE 3-5
      is positive, and blocking it costs trades for no measured reason. NIFTY expires
      Tuesday, so [0,5] excludes only a Wednesday entry.

UNCHANGED, deliberately:
  QG-O4 (+Rs1,768, b=2.48)  DTE 0 +2,211 | 1-2 +3,491 | 3-5 -2,568 | 6+ -1,366. [0,2] is
      already exactly right for it — it is the book's one clean winner and the only seller
      whose wins are bigger than its losses.
  IDX NIFTY VRP Call-Spread (+Rs125, n=18) DTE 0 +1,283 | 1-2 -2,406 | 3-5 +1,248. The
      sign alternates by bucket, which is noise, not signal — no honest window exists.
      Left at [0,2] to let the tighter 0.45 stop play out.

HONEST LIMIT: the re-gates are fitted to each strategy's own past trades, with the DTE 6+
buckets resting on n=2-3. The DIRECTION is corroborated by the 259-trade aggregate gradient
and by QG-O4/RAE-NIFTY independently, but these are not controlled experiments. Both re-gated
strategies still owe the SS13.5 ladder. Nothing here creates edge.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

RETIRE = {
    "rae-range-seller-sensex":
        "n=30, -Rs6,413, 0 of 4 DTE buckets positive, breakeven 68% vs WR 47%",
    "idx-sensex-putspread-0001":
        "n=51, -Rs8,830, only positive bucket is n=9 noise; worst buckets are DTE 0-2",
}
REGATE = {
    "f390da9d-8a38-4bf6-87d9-0e560eb852e5": (0, 5, "QG-O1: DTE 6+ is the entire loss (-Rs6,498); had NO window"),
    "rae-range-seller-nifty": (0, 5, "RAE NIFTY: DTE 6+ is the entire loss (-Rs1,736); own DTE 3-5 is positive"),
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    client = AsyncIOMotorClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))
    db = client[os.environ.get("DB_NAME", "quantg")]
    now = datetime.now(timezone.utc).isoformat()
    print(f"{'APPLY' if args.apply else 'DRY RUN'}\n")

    print("--- RETIRE ---")
    for sid, why in RETIRE.items():
        s = await db.strategies.find_one({"id": sid}, {"name": 1, "status": 1, "_id": 0})
        if not s:
            print(f"  {sid}: NOT FOUND\n")
            continue
        open_n = await db.strategy_positions.count_documents(
            {"strategy_id": sid, "status": {"$in": ["OPEN", "FILLED", "PENDING_BROKER", "EXITING"]}})
        print(f"  {s.get('name')}  status={s.get('status')} -> archived")
        print(f"      {why}")
        print(f"      open positions left to close normally: {open_n}")
        if args.apply:
            r = await db.strategies.update_one({"id": sid}, {"$set": {
                "status": "archived",
                "archived_at": now,
                "archived_reason": why,
                # belt and braces: these keep a founder-forced row from being re-woken
                "founder_forced_live": False,
                "manual_paused": True,
                "updated_at": now,
            }})
            print(f"      => archived (matched={r.matched_count})")
        print()

    print("--- RE-GATE ---")
    for sid, (lo, hi, why) in REGATE.items():
        s = await db.strategies.find_one({"id": sid}, {"name": 1, "visual_config": 1, "_id": 0})
        if not s:
            print(f"  {sid}: NOT FOUND\n")
            continue
        o = (s.get("visual_config") or {}).get("options") or {}
        print(f"  {s.get('name')}")
        print(f"      dte_window [{o.get('min_dte_days')},{o.get('max_dte_days')}] -> [{lo},{hi}]")
        print(f"      {why}")
        if args.apply:
            r = await db.strategies.update_one({"id": sid}, {"$set": {
                "visual_config.options.min_dte_days": lo,
                "visual_config.options.max_dte_days": hi,
                "visual_config.options.geometry_change_note": why,
                "visual_config.options.geometry_changed_at": now,
                "updated_at": now,
            }})
            print(f"      => written (matched={r.matched_count})")
        print()

    live = await db.strategies.count_documents({"status": {"$in": ["live", "active"]}})
    print(f"live/active strategies after this: {live}")
    if not args.apply:
        print("\nre-run with --apply to write.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
