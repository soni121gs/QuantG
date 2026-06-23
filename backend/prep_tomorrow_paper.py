import os
import sys
import asyncio
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

# Load env file
backend_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(backend_dir, ".env"))

async def prep_tomorrow_paper():
    print("=== STARTING PAPER-READY STATE PREPARATION ===")
    
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    if "mongo:" in mongo_url:
        mongo_url = mongo_url.replace("mongo:", "localhost:")
    db_name = os.environ.get("DB_NAME", "quantg")
    
    print(f"Connecting to MongoDB: {mongo_url} [DB: {db_name}]")
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    
    # 1. Clear all paper orders (allows clean daily trade limits and P&L tomorrow)
    print("Purging paper orders...")
    res_orders = await db.orders.delete_many({"mode": "paper"})
    print(f"  Deleted {res_orders.deleted_count} stale paper orders.")
    
    # 2. Clear all strategy positions for paper mode
    print("Purging paper strategy positions...")
    res_sp = await db.strategy_positions.delete_many({"mode": "paper"})
    print(f"  Deleted {res_sp.deleted_count} stale paper strategy positions.")
    
    # 3. Clear all open paper positions from main positions collections
    print("Purging paper broker positions...")
    res_pos = await db.positions.delete_many({"mode": "paper"})
    print(f"  Deleted {res_pos.deleted_count} stale paper broker positions.")
    
    # 4. Remove active locks and reservations
    print("Purging active strategy locks & reservations...")
    res_locks = await db.strategy_position_locks.delete_many({})
    print(f"  Deleted {res_locks.deleted_count} active position locks.")
    
    # 5. Purge old pending signals to prevent stale signals triggering tomorrow
    print("Purging stale signals...")
    res_sig = await db.signals.delete_many({})
    print(f"  Deleted {res_sig.deleted_count} historical/stale signals.")
    
    # 6. Reset daily paper trading stats/history
    print("Purging paper trading daily history & stats...")
    res_stats = await db.paper_trading_history.delete_many({})
    print(f"  Deleted {res_stats.deleted_count} stale paper stats.")
    
    # 7. Configure user accounts: Default to paper_mode = True and disable Live trading by default
    print("Configuring user account safety flags...")
    users = await db.users.find({}).to_list(100)
    for user in users:
        uid = user["id"]
        email = user["email"]
        print(f"  Setting {email} ({uid}) -> paper_mode = True, Live Disabled.")
        await db.users.update_one(
            {"id": uid},
            {"$set": {
                "paper_mode": True,
                "execution_broker": "upstox",
                "data_broker": "upstox"
            }}
        )
        
        # 8. Setup Strategy Configuration: Enable exactly 1 stable strategy and pause the rest
        print(f"  Configuring strategies for user: {email}...")
        
        # Disable all strategies first
        await db.strategies.update_many(
            {"user_id": uid},
            {"$set": {
                "status": "paused",
                "mode": "paper",
                "broker": "upstox",
                "last_signal_at": None,
                "last_error": None,
                "last_signal_validation": None
            }}
        )
        
        # Enable NIFTY Momentum Buyer as live in paper mode
        nifty_momentum_buyer_name = "NIFTY Momentum Buyer"
        target_strat = await db.strategies.find_one({
            "user_id": uid,
            "name": nifty_momentum_buyer_name
        })
        
        if target_strat:
            print(f"    Enabling stable strategy: '{nifty_momentum_buyer_name}' in PAPER mode.")
            await db.strategies.update_one(
                {"id": target_strat["id"]},
                {"$set": {
                    "status": "live",
                    "mode": "paper",
                    "broker": "upstox"
                }}
            )
        else:
            print(f"    WARNING: '{nifty_momentum_buyer_name}' template not found. Seeding strategies...")
            sys.path.append(backend_dir)
            try:
                from server import seed_default_strategies_for_user
                await seed_default_strategies_for_user(uid)
                # Retry enable
                target_strat = await db.strategies.find_one({
                    "user_id": uid,
                    "name": nifty_momentum_buyer_name
                })
                if target_strat:
                    print(f"      Enabling seeded '{nifty_momentum_buyer_name}' in PAPER mode.")
                    await db.strategies.update_one(
                        {"id": target_strat["id"]},
                        {"$set": {
                            "status": "live",
                            "mode": "paper",
                            "broker": "upstox"
                        }}
                    )
            except Exception as e:
                print(f"      Failed to seed/enable default strategy: {e}")
                
    print("=== PAPER-READY STATE PREPARATION COMPLETE ===")

if __name__ == "__main__":
    asyncio.run(prep_tomorrow_paper())
