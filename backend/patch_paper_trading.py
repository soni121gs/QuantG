# Patch to add paper trading P&L tracking
# Run this once to add the collection and backfill indexes

import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import os
from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

async def patch():
    mongo_url = os.environ['MONGO_URL']
    client = AsyncIOMotorClient(mongo_url)
    db = client[os.environ['DB_NAME']]
    
    # Create paper_trading_history collection with indexes
    try:
        await db.create_collection("paper_trading_history")
        print("✓ Created paper_trading_history collection")
    except Exception as e:
        print(f"Collection may already exist: {e}")
    
    # Create indexes
    try:
        await db.paper_trading_history.create_index([("user_id", 1), ("created_at", -1)])
        print("✓ Created index on paper_trading_history")
    except Exception as e:
        print(f"Index creation: {e}")
    
    client.close()

if __name__ == "__main__":
    asyncio.run(patch())
