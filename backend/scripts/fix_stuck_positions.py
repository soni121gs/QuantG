"""
FIX 8: Migration script — repair stuck positions with null instrument_key.

Finds all positions where:
  - instrument_key is null AND
  - status is one of: OPEN, CIRCUIT_BREAKER, EXITING, FILLED, LTP_UNAVAILABLE

For each stuck position:
  1. Try to reconstruct instrument_key from (underlying, strike, expiry, option_type)
     by looking up db.upstox_instruments.
  2. If found: update instrument_key, reset status to OPEN, clear exit_attempts,
     exit_order_failures so the monitor can pick it up again.
  3. If not found: mark status as MANUAL_REVIEW.

Prints a full report at the end.

Usage (from inside the Docker container or locally):
    python scripts/fix_stuck_positions.py [--dry-run]
"""
import asyncio
import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient


STUCK_STATUSES = {"OPEN", "CIRCUIT_BREAKER", "EXITING", "FILLED"}
MONGO_URI = os.environ.get("MONGO_URI") or os.environ.get("MONGO_URL") or "mongodb://mongo:27017"
DB_NAME = os.environ.get("MONGO_DB_NAME", "quantg")


async def find_instrument_key(
    db,
    underlying: str,
    strike: Any,
    expiry: str,
    option_type: str,
    trading_symbol: str,
) -> Optional[str]:
    """Try to look up instrument_key from the Upstox instruments master."""
    # Strategy 1: direct tradingsymbol lookup (most reliable)
    if trading_symbol:
        doc = await db.upstox_instruments.find_one(
            {"tradingsymbol": trading_symbol},
            {"instrument_key": 1, "_id": 0},
        )
        if doc and doc.get("instrument_key"):
            return doc["instrument_key"]

    # Strategy 2: match by underlying + strike + expiry + option_type
    if underlying and strike and expiry and option_type:
        query: Dict[str, Any] = {
            "underlying_symbol": underlying.upper(),
            "option_type": option_type.upper(),
        }
        try:
            query["strike"] = float(strike)
        except (TypeError, ValueError):
            query["strike"] = strike

        # expiry can be "2026-06-12" or "12-JUN-26"
        exp_str = str(expiry)
        doc = await db.upstox_instruments.find_one(
            {**query, "expiry": {"$regex": exp_str[:10], "$options": "i"}},
            {"instrument_key": 1, "_id": 0},
        )
        if doc and doc.get("instrument_key"):
            return doc["instrument_key"]

    return None


async def run_migration(dry_run: bool = False) -> None:
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    now_iso = datetime.now(timezone.utc).isoformat()

    print(f"\n{'='*60}")
    print(f"FIX 8: Stuck Position Migration  [{now_iso}]")
    print(f"Mode: {'DRY RUN (no changes)' if dry_run else 'LIVE (writing to DB)'}")
    print(f"{'='*60}\n")

    # Find all stuck positions with null instrument_key
    stuck: List[Dict[str, Any]] = await db.strategy_positions.find(
        {
            "instrument_key": {"$in": [None, "", "null"]},
            "status": {"$in": list(STUCK_STATUSES)},
        },
        {"_id": 0},
    ).to_list(500)

    print(f"Found {len(stuck)} stuck position(s) with null instrument_key\n")

    fixed = []
    manual_review = []
    skipped = []

    for pos in stuck:
        pos_id   = pos.get("id", "?")
        symbol   = pos.get("target_symbol") or pos.get("trading_symbol") or pos.get("symbol", "?")
        status   = pos.get("status", "?")
        user_id  = pos.get("user_id", "?")
        underlying = pos.get("underlying") or pos.get("symbol_group") or ""
        strike     = pos.get("strike")
        expiry     = pos.get("expiry")
        opt_type   = pos.get("option_type")
        exit_att   = pos.get("exit_attempts", 0)
        exit_ord_f = pos.get("exit_order_failures", 0)

        print(f"  pos_id={pos_id}  symbol={symbol}  status={status}  user={user_id[:8]}")
        print(f"    underlying={underlying}  strike={strike}  expiry={expiry}  opt_type={opt_type}")

        # Try to find the instrument_key
        ikey = await find_instrument_key(
            db,
            underlying=underlying,
            strike=strike,
            expiry=str(expiry) if expiry else "",
            option_type=str(opt_type) if opt_type else "",
            trading_symbol=symbol,
        )

        if ikey:
            print(f"    ✓ FOUND instrument_key={ikey}")
            if not dry_run:
                result = await db.strategy_positions.update_one(
                    {"id": pos_id},
                    {"$set": {
                        "instrument_key": ikey,
                        "instrument_token": ikey,
                        "status": "OPEN",
                        "exit_attempts": 0,
                        "exit_order_failures": 0,
                        "exit_data_failures": 0,
                        "last_error": None,
                        "updated_at": now_iso,
                        "migration_fixed_at": now_iso,
                    }},
                )
                matched = result.matched_count
            else:
                matched = 1
            fixed.append({
                "pos_id": pos_id, "symbol": symbol, "status": status,
                "instrument_key": ikey, "updated": matched,
            })
        else:
            print(f"    ✗ COULD NOT reconstruct instrument_key → MANUAL_REVIEW")
            if not dry_run:
                await db.strategy_positions.update_one(
                    {"id": pos_id},
                    {"$set": {
                        "status": "MANUAL_REVIEW",
                        "last_error": "Migration: instrument_key not found in upstox_instruments",
                        "updated_at": now_iso,
                        "migration_manual_at": now_iso,
                    }},
                )
            manual_review.append({
                "pos_id": pos_id, "symbol": symbol, "status": status,
                "underlying": underlying, "strike": strike,
                "expiry": expiry, "opt_type": opt_type,
            })
        print()

    print(f"{'='*60}")
    print(f"MIGRATION REPORT")
    print(f"{'='*60}")
    print(f"  Total stuck positions found : {len(stuck)}")
    print(f"  Fixed (instrument_key found): {len(fixed)}")
    print(f"  Needs manual review         : {len(manual_review)}")
    print(f"  Skipped                     : {len(skipped)}")

    if fixed:
        print(f"\n  FIXED positions:")
        for f in fixed:
            print(f"    {f['pos_id']}  {f['symbol']}  → {f['instrument_key']}")

    if manual_review:
        print(f"\n  MANUAL REVIEW positions (no instrument_key found):")
        for m in manual_review:
            print(f"    {m['pos_id']}  {m['symbol']}  (underlying={m['underlying']}, "
                  f"strike={m['strike']}, expiry={m['expiry']}, type={m['opt_type']})")
        print(f"\n  To resolve: find the correct instrument_key in db.upstox_instruments")
        print(f"  and manually set it, then set status=OPEN and exit_order_failures=0")

    if dry_run:
        print(f"\n  [DRY RUN] No changes were written. Re-run without --dry-run to apply.")
    else:
        print(f"\n  Migration complete. Fixed positions will be picked up by")
        print(f"  the position monitor on the next 30-second tick.")

    print(f"{'='*60}\n")
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fix stuck positions with null instrument_key")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing to DB")
    args = parser.parse_args()
    asyncio.run(run_migration(dry_run=args.dry_run))
