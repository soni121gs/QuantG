"""Clean-slate migration — 2026-06-10 unified accounting spine reset.

Run ONCE after deploying the spine/exit-guarantee/deadlock fixes.
Wipes all paper trading state so the new single-ledger accounting starts
from a verifiably clean baseline:

  1. Reset every paper wallet to ₹5,00,000 (the corrupted one included).
  2. Wipe trading collections: strategy_positions, positions, orders,
     signals, fills, trade_fills, trades, processed_fill_ids,
     paper_wallet_credits, option_selector_decisions.
  3. Reset strategy scorecards (today_pnl/total_pnl/total_trades/wins/losses)
     — all strategies KEEP their status (the 9 live ones stay live).
  4. Ensure unique indexes the idempotency guards depend on:
     processed_fill_ids.fill_id, paper_wallet_credits.order_id.

Usage (inside backend container or venv with backend/.env loaded):
    python scripts/migration_2026_06_10_clean_slate.py            # dry run
    python scripts/migration_2026_06_10_clean_slate.py --apply    # execute
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

PAPER_INITIAL_BALANCE = 500_000.0

WIPE_COLLECTIONS = [
    "strategy_positions",
    "positions",
    "orders",
    "signals",
    "fills",
    "trade_fills",
    "trades",
    "processed_fill_ids",
    "paper_wallet_credits",
    "option_selector_decisions",
]

SCORECARD_RESET = {
    "today_pnl": 0.0,
    "total_pnl": 0.0,
    "last_pnl": 0.0,
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "last_error": "",
}


async def main(apply: bool) -> None:
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    db = AsyncIOMotorClient(mongo_url)[db_name]
    mode = "APPLY" if apply else "DRY RUN"
    print(f"=== Clean-slate migration [{mode}] db={db_name} ===")

    # 1. Wallets
    wallets = await db.paper_wallets.find({}, {"user_id": 1, "balance": 1, "_id": 0}).to_list(100)
    now = datetime.now(timezone.utc).isoformat()
    for w in wallets:
        print(f"  wallet user={w['user_id']} balance={w.get('balance')} -> {PAPER_INITIAL_BALANCE}")
        if apply:
            await db.paper_wallets.replace_one(
                {"user_id": w["user_id"]},
                {
                    "user_id": w["user_id"],
                    "balance": PAPER_INITIAL_BALANCE,
                    "initial_balance": PAPER_INITIAL_BALANCE,
                    "total_debited": 0.0,
                    "total_credited": 0.0,
                    "created_at": now,
                    "updated_at": now,
                },
                upsert=True,
            )

    # 2. Wipe trading collections
    for coll in WIPE_COLLECTIONS:
        count = await db[coll].count_documents({})
        print(f"  wipe {coll}: {count} docs")
        if apply and count:
            await db[coll].delete_many({})

    # 3. Strategy scorecards (status untouched — live strategies stay live)
    n_strats = await db.strategies.count_documents({})
    print(f"  reset scorecards on {n_strats} strategy docs (status preserved)")
    if apply:
        await db.strategies.update_many({}, {"$set": SCORECARD_RESET})

    # 4. Idempotency-critical unique indexes
    if apply:
        await db.processed_fill_ids.create_index("fill_id", unique=True)
        await db.paper_wallet_credits.create_index("order_id", unique=True)
        print("  ensured unique indexes: processed_fill_ids.fill_id, paper_wallet_credits.order_id")
    else:
        print("  would ensure unique indexes: processed_fill_ids.fill_id, paper_wallet_credits.order_id")

    # Verification snapshot
    live = await db.strategies.count_documents({"status": "live"})
    print(f"  strategies with status=live after migration: {live}")
    print(f"=== done [{mode}] ===")


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
