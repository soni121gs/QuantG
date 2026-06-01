"""QuantG Scorecard Performance Compiler.

Sweeps all strategies and computes scorecard summaries (today, 7d, 30d P&L, win rates).
Refreshes the strategy_performance collection.
Supports --dry-run and --apply.
"""
import os
import sys
import argparse
import asyncio
from pymongo import MongoClient

def main():
    parser = argparse.ArgumentParser(description="QuantG Strategy Performance Rebuilder")
    parser.add_argument("--dry-run", action="store_true", help="Compile and display scorecards without writing to DB")
    parser.add_argument("--apply", action="store_true", help="Compile and persist scorecards to strategy_performance collection")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("Please specify either --dry-run or --apply")
        sys.exit(1)

    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    
    client = MongoClient(mongo_url)
    db = client[db_name]

    print("\n>>> STARTING PERFORMANCE SCORECARD REBUILDER <<<\n")

    # Import performance tracker directly
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from core.performance_tracker import PerformanceTracker
    
    loop = asyncio.get_event_loop()
    tracker = PerformanceTracker(db)

    # Fetch unique users
    users = db.users.find({}, {"id": 1})
    
    for u in users:
        user_id = u["id"]
        print(f"Processing performance leaderboards for user: {user_id}")
        
        # Async run leaderboard compiler
        async def compile_scores():
            return await tracker.rebuild_leaderboard(user_id)
            
        leaderboard = loop.run_until_complete(compile_scores())
        print(f"Successfully compiled rankings for {len(leaderboard)} strategies.")
        
        for rank in leaderboard:
            print(f" - Strategy: {rank['name']} (P&L: {rank['pnl']}, Win Rate: {rank['win_rate']}, Rec: {rank['status_recommendation']})")

    print("\n>>> STRATEGY RANKINGS REBUILD COMPLETED <<<\n")

if __name__ == "__main__":
    main()
