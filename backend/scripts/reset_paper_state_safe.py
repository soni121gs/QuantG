import os
import sys
import pymongo

def main():
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    
    confirm = "--confirm" in sys.argv
    
    print("=" * 60)
    print("        SAFE PAPER STATE RESET TOOL")
    print("=" * 60)
    print(f"Connecting to: {mongo_url}")
    print(f"Database Name: {db_name}")
    print(f"Dry Run Mode: {not confirm}")
    print("-" * 60)
    
    try:
        client = pymongo.MongoClient(mongo_url, serverSelectionTimeoutMS=2000)
        client.admin.command('ping')
        db = client[db_name]
    except Exception as e:
        print(f"Error connecting to MongoDB: {e}")
        sys.exit(1)
        
    # We will safely target ONLY paper mode records:
    # 1. Delete orders where mode = "paper"
    # 2. Delete positions where mode = "paper"
    # 3. Delete fills where mode = "paper"
    # 4. Delete skipped signals
    # 5. Reset paper wallets
    
    if not confirm:
        print("[DRY-RUN] Will delete paper orders (mode='paper')")
        print("[DRY-RUN] Will delete paper strategy positions (mode='paper')")
        print("[DRY-RUN] Will delete paper fills (mode='paper')")
        print("[DRY-RUN] Will delete all skipped signals (mode='paper' or general)")
        print("[DRY-RUN] Will delete paper wallets (db.paper_wallets)")
        print("\nTo execute this action, run the command with the --confirm flag:")
        print("  python backend/scripts/reset_paper_state_safe.py --confirm")
        sys.exit(0)
        
    print("Deleting paper orders...")
    res_orders = db.orders.delete_many({"mode": "paper"})
    print(f"-> Deleted {res_orders.deleted_count} paper orders.")
    
    print("Deleting paper positions...")
    res_pos = db.strategy_positions.delete_many({"mode": "paper"})
    print(f"-> Deleted {res_pos.deleted_count} paper positions.")
    
    print("Deleting paper fills...")
    res_fills = db.fills.delete_many({"mode": "paper"})
    print(f"-> Deleted {res_fills.deleted_count} paper fills.")
    
    print("Deleting skipped signals...")
    res_skips = db.skipped_signals.delete_many({"mode": "paper"})
    res_skips_none = db.skipped_signals.delete_many({"mode": {"$exists": False}})
    print(f"-> Deleted {res_skips.deleted_count + res_skips_none.deleted_count} skipped signals.")
    
    print("Resetting paper wallets...")
    res_wallets = db.paper_wallets.delete_many({})
    print(f"-> Deleted {res_wallets.deleted_count} paper wallet balances. New ones will auto-initialize to Rs. 5,00,000 on next transaction.")
    
    print("\nSafe paper state reset completed successfully!")

if __name__ == "__main__":
    main()
