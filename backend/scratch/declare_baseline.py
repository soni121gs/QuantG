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
    if "mongo:" in url or "@mongo" in url:
         # running inside docker, localhost may not resolve to mongodb container
         pass
    else:
         # fallback / default to container name if running in production docker environment
         # but let's default to standard localhost unless overridden by env.
         pass

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
        "UPSTOX NIFTY ATM Momentum",
        "NIFTY Quick EMA",
        "UPSTOX BANKNIFTY ATM Breakout"
    ]
    
    print("\n2. Pausing underperforming single-leg buyer strategies:")
    for name in to_pause:
        strategy = db.strategies.find_one({"name": name})
        if not strategy:
            print(f"   [WARNING] Strategy not found by name: '{name}'")
            continue
        
        # Pause the strategy
        db.strategies.update_one(
            {"id": strategy["id"]},
            {"$set": {"status": "paused", "updated_at": datetime.utcnow().isoformat()}}
        )
        updated = db.strategies.find_one({"id": strategy["id"]})
        print(f"   Updated status of '{name}' (id={strategy['id']}) to: '{updated.get('status')}'")

    print("\nBaseline epoch and strategy trims declared successfully!")

if __name__ == "__main__":
    main()
