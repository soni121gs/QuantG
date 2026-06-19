"""
Declare clean research baseline epoch and pause the three underperforming strategies.

Run inside the backend container:
    docker-compose exec -T backend python scratch/declare_baseline.py
"""
import os
import sys
from datetime import datetime
from pymongo import MongoClient

def main():
    url = os.environ.get("MONGO_URL") or os.environ.get("MONGODB_URI") or "mongodb://localhost:27017"
    
    # Try connecting
    print(f"Connecting to MongoDB at: {url}")
    client = MongoClient(url)
    db = client["quantg"]

    # 1. Declare baseline date
    baseline_doc = {
        "_id": "research_baseline",
        "baseline_date": "2026-06-19T09:15:00+05:30",
        "updated_at": datetime.utcnow().isoformat()
    }
    
    print("\n1. Declaring research baseline epoch...")
    db.app_config.replace_one({"_id": "research_baseline"}, baseline_doc, upsert=True)
    stored = db.app_config.find_one({"_id": "research_baseline"})
    print(f"   Successfully set app_config research_baseline: {stored}")

    # 2. Pause/trim the three underperforming single-leg buyer strategies
    to_pause = [
        "UPSTOX NIFTY ATM Option Momentum Buyer",
        "NIFTY Quick EMA Scalper",
        "UPSTOX BANKNIFTY ATM Option Breakout Buyer"
    ]
    
    print("\n2. Pausing underperforming single-leg buyer strategies:")
    for name in to_pause:
        # Find all documents with this name (could be multiple if drafts exist)
        strategies = list(db.strategies.find({"name": name}))
        if not strategies:
            print(f"   [WARNING] Strategy not found by name: '{name}'")
            continue
        
        for strategy in strategies:
            # Set structure to debit_spread for the NIFTY Momentum Buyer as part of TASK-048
            if name == "UPSTOX NIFTY ATM Option Momentum Buyer":
                db.strategies.update_one(
                    {"id": strategy["id"]},
                    {"$set": {
                        "status": "paused",
                        "visual_config.options.structure": "debit_spread",
                        "updated_at": datetime.utcnow().isoformat()
                    }}
                )
                print(f"   Converted '{name}' (id={strategy['id']}) to debit_spread and paused.")
            else:
                db.strategies.update_one(
                    {"id": strategy["id"]},
                    {"$set": {
                        "status": "paused",
                        "updated_at": datetime.utcnow().isoformat()
                    }}
                )
                print(f"   Paused '{name}' (id={strategy['id']}).")

    print("\nBaseline epoch and strategy trims declared successfully!")

if __name__ == "__main__":
    main()
