import os
import sys
import asyncio
import sqlite3
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

# Load env file
backend_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(backend_dir, ".env"))

async def reset_db():
    print("=== STARTING PURE UPSTOX DATABASE RESET ===")
    
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    
    print(f"Connecting to MongoDB: {mongo_url} [DB: {db_name}]")
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    
    # Collections to purge completely
    collections_to_purge = ["orders", "positions", "paper_trading_history", "strategies"]
    for coll in collections_to_purge:
        print(f"Purging MongoDB collection: {coll}...")
        await db[coll].delete_many({})
    
    # Remove any Zerodha API keys from api_keys collection, leaving only upstox
    print("Purging Zerodha API keys from MongoDB...")
    await db["api_keys"].delete_many({"broker": {"$ne": "upstox"}})
    
    # Clean SQLite runtime ledger cache
    sqlite_path = os.path.join(backend_dir, "runtime_state.sqlite3")
    if os.path.exists(sqlite_path):
        print(f"Purging SQLite cache database: {sqlite_path}...")
        try:
            conn = sqlite3.connect(sqlite_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [row[0] for row in cursor.fetchall()]
            for table in tables:
                print(f"  Clearing SQLite table: {table}")
                cursor.execute(f"DELETE FROM {table};")
            conn.commit()
            conn.close()
            print("SQLite database successfully cleared.")
        except Exception as e:
            print(f"Error clearing SQLite database: {e}")
    else:
        print("SQLite runtime cache not found. Skipping.")
        
    # Import the default seeding from server
    sys.path.append(backend_dir)
    try:
        from server import seed_default_strategies_for_user
        
        # Get all registered users
        users = await db["users"].find({}).to_list(100)
        print(f"Found {len(users)} registered users.")
        
        for user in users:
            uid = user.get("id")
            email = user.get("email")
            print(f"Seeding high-performance default strategies for user: {email} ({uid})...")
            
            # Reset user paper mode to False so they start in LIVE mode directly
            await db["users"].update_one({"id": uid}, {"$set": {"paper_mode": False, "execution_broker": "upstox", "data_broker": "upstox"}})
            
            inserted_count = await seed_default_strategies_for_user(uid)
            print(f"Seeded {inserted_count} strategies.")
            
            # Force all strategies to natively use Upstox and default to LIVE (regular algo) mode!
            print("Configuring all seeded strategies for pure Upstox Live (regular algo) execution...")
            res = await db["strategies"].update_many(
                {"user_id": uid},
                {"$set": {
                    "broker": "upstox",
                    "mode": "live"
                }}
            )
            print(f"Updated {res.modified_count} strategies to Upstox Live.")
            
    except Exception as e:
        print(f"Error running strategy seeding: {e}")
        
    print("=== PURE UPSTOX DATABASE RESET COMPLETE ===")

if __name__ == "__main__":
    asyncio.run(reset_db())
