#!/usr/bin/env python3
"""Monday book cutover (2026-08-01) — curate the paused 13-row book into the
paper-forward book for the next session. Idempotent; DRY-RUN by default (--apply).

Decisions (all evidence-backed this session; CLAUDE.md §21/§23 discipline):
  ARCHIVE (proven dead / structurally broken):
    - QG-O11 NIFTY Regime Seller Credit Scalp  — ratio 0.255, −₹8,075 lifetime;
      width-1 scalp spends ~100% of edge on friction (§21.9 winning band 0.12–0.16).
    - RAE BANKNIFTY Range Seller               — BANKNIFTY is monthly-expiry only, so
      theta reachability vetoes an intraday seller at any width (§21.2 corollary).
  ACTIVATE (status=live in PAPER; survivors + deep-ITM trend riders):
    - QG-O1, RAE NIFTY/SENSEX Range Sellers, IDX VRP ×2, IDX debit ×2, QG-O4,
      RAE NIFTY/SENSEX/BANKNIFTY Trend Delta-1 (deep-ITM ITM_BUY, IV-cheap gated).

NOT seeded here: the long-vol/tail overlay. Its validated structure is a ~5% OTM put
held to expiry, but the LIVE single-leg selector only resolves ATM/ITM1/OTM1 — it
cannot express a far-OTM tail leg, and an OTM1 approximation is unvalidated. The
research sleeve (core/long_vol_tail.py) is committed; the live far-OTM selector path
is the next build. Seeding a half-built hedge would be a footgun (no-error mandate).

CORE_ENGINE_LIVE_ENABLED stays false (real money OFF). Router/forward-paper is enabled
separately via RAE_ROUTER_ENABLED=true (paper) in docker-compose.

Run in-container:
    docker exec quantg-backend python /app/scripts/monday_book_cutover.py            # dry-run
    docker exec quantg-backend python /app/scripts/monday_book_cutover.py --apply
"""
import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /app

import pymongo  # noqa: E402
from core.strategy_registry import validate_strategy_doc  # noqa: E402

ARCHIVE = [
    "QG-O11 NIFTY Regime Seller Credit Scalp",
    "RAE BANKNIFTY Range Seller (RANGE/INSIDE)",
]

ACTIVATE = [
    "QG-O1 NIFTY Put Spread Theta Core",
    "RAE NIFTY Range Seller (RANGE/INSIDE)",
    "RAE SENSEX Range Seller (RANGE/INSIDE)",
    "IDX NIFTY VRP Call-Spread (RANGE+rich)",
    "IDX SENSEX VRP Put-Spread (RANGE+rich)",
    "IDX NIFTY Long-Gamma (HIGH_VOL debit)",
    "IDX NIFTY Mean-Reversion Fade (debit)",
    "QG-O4 SENSEX Call Spread Range Pilot",
    "RAE NIFTY Trend Delta-1 (TREND)",
    "RAE SENSEX Trend Delta-1 (TREND)",
    "RAE BANKNIFTY Trend Delta-1 (TREND)",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()
    db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
                             serverSelectionTimeoutMS=5000)[os.environ.get("DB_NAME", "quantg")]
    now = datetime.now(timezone.utc).isoformat()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Monday book cutover [{mode}] ===\n")

    # 1) pre-flight coherence check on every row we're about to activate — abort on error
    problems = []
    for name in ACTIVATE:
        doc = db.strategies.find_one({"name": name})
        if not doc:
            problems.append(f"MISSING: {name}")
            continue
        v = validate_strategy_doc(doc)
        flag = "OK " if v.ok else "ERR"
        print(f"  [{flag}] {name}")
        if v.errors:
            problems.append(f"{name}: {v.errors}")
        for w in v.warnings:
            print(f"         warn: {w}")
    if any(p.startswith(("MISSING", )) or ": [" in p for p in problems):
        print("\nCOHERENCE PROBLEMS — aborting, no writes:")
        for p in problems:
            print("   " + p)
        return 1

    # 2) archive the dead rows
    print("\n-- archive --")
    for name in ARCHIVE:
        doc = db.strategies.find_one({"name": name})
        if not doc:
            print(f"  (absent) {name}")
            continue
        print(f"  ARCHIVE  {name}  (was {doc.get('status')})")
        if args.apply:
            db.strategies.update_one({"_id": doc["_id"]}, {"$set": {
                "status": "archived", "manual_paused": True, "schedule_paused": True,
                "archived_at": now,
            }})

    # 3) ARM the survivor + trend book for the Monday 09:15 IST auto-wake (paper).
    # The app parks live rows to paused+schedule_paused=True at 15:35 and wakes
    # paused+schedule_paused=True+manual_paused!=True to status=live at 09:15 Mon-Fri
    # (server.py ~17268). So the correct weekend state is ARMED, not forced-live —
    # this avoids live-evaluating over a closed weekend and uses the app's own cycle.
    print("\n-- arm for Monday 09:15 IST wake (paper) --")
    for name in ACTIVATE:
        doc = db.strategies.find_one({"name": name})
        print(f"  ARMED    {name}  (was {doc.get('status')}, "
              f"manual_paused={doc.get('manual_paused')})")
        if args.apply:
            db.strategies.update_one({"_id": doc["_id"]}, {"$set": {
                "status": "paused", "schedule_paused": True, "manual_paused": False,
                "archived_at": None,
            }})

    # 4) summary
    print("\n-- resulting book --")
    for s in db.strategies.find({}, {"name": 1, "status": 1}).sort("name", 1):
        print(f"  {s.get('status',''):>9}  {s['name']}")
    print(f"\n{mode} complete. CORE_ENGINE_LIVE_ENABLED must stay false (paper).")
    if not args.apply:
        print("Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
