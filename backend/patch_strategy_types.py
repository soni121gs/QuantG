#!/usr/bin/env python3
"""
Add strategy type/category support (Equity, Options, Futures)
and improve backtest result storage.
"""
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient
import os
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime, timezone

ROOT_DIR = Path(__file__).parent.parent
load_dotenv(ROOT_DIR / '.env')

async def patch():
    mongo_url = os.environ['MONGO_URL']
    client = AsyncIOMotorClient(mongo_url)
    db = client[os.environ['DB_NAME']]
    
    # 1. Add asset_class field to existing strategies
    print("[1] Adding asset_class field to strategies...")
    result = await db.strategies.update_many(
        {"asset_class": {"$exists": False}},
        {"$set": {"asset_class": "equity"}}
    )
    print(f"    Updated {result.modified_count} strategies")
    
    # 2. Create backtest_results collection
    try:
        await db.create_collection("backtest_results")
        print("[2] Created backtest_results collection")
    except:
        print("[2] backtest_results collection already exists")
    
    # 3. Create indexes for backtest_results
    await db.backtest_results.create_index([("user_id", 1), ("strategy_id", 1)])
    await db.backtest_results.create_index([("user_id", 1), ("created_at", -1)])
    await db.backtest_results.create_index([("asset_class", 1), ("user_id", 1)])
    print("[3] Created indexes for backtest_results")
    
    # 4. Ensure paper_trading_history has asset_class
    result = await db.paper_trading_history.update_many(
        {"asset_class": {"$exists": False}},
        {"$set": {"asset_class": "equity"}}
    )
    print(f"[4] Added asset_class to paper_trading_history ({result.modified_count} docs)")
    
    client.close()
    print("\n[OK] Patch complete!")

if __name__ == "__main__":
    asyncio.run(patch())
