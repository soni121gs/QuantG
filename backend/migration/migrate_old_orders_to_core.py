"""QuantG Legacy Order Migrator.

Scans older order records in the orders collection and normalizes schemas.
Adds mode (paper/live) based on legacy flags if missing.
Supports --dry-run and --apply modes.
"""
import os
import sys
import argparse
from pymongo import MongoClient

def main():
    parser = argparse.ArgumentParser(description="QuantG Legacy Order Migrator")
    parser.add_argument("--dry-run", action="store_true", help="Inspect and report issues without mutating data")
    parser.add_argument("--apply", action="store_true", help="Apply updates and repair legacy order documents")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("Please specify either --dry-run or --apply")
        sys.exit(1)

    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    
    client = MongoClient(mongo_url)
    db = client[db_name]

    print("\n>>> STARTING LEGACY ORDERS SCHEMA MIGRATION <<<\n")

    cursor = db.orders.find({"mode": {"$exists": False}})
    legacy_count = 0
    updated_count = 0

    for doc in cursor:
        legacy_count += 1
        
        # Derive mode
        is_paper = True
        if doc.get("broker") == "upstox" or doc.get("legacy_status") == "PENDING_BROKER":
            is_paper = False
            
        mode_val = "paper" if is_paper else "live"
        
        if args.apply:
            db.orders.update_one(
                {"_id": doc["_id"]},
                {"$set": {
                    "mode": mode_val,
                    "updated_at": doc.get("updated_at") or doc.get("created_at")
                }}
            )
            updated_count += 1
        else:
            print(f"[*] Dry-run: Document {doc.get('id') or doc.get('_id')} has no mode. Recommendation: set mode to '{mode_val}'.")

    print(f"\nMigration sweep completed. Inspected legacy orders: {legacy_count}. Modified: {updated_count}.")
    print("\n>>> MIGRATION SUMMARY COMPLETED <<<\n")

if __name__ == "__main__":
    main()
