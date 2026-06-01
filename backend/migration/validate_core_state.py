"""QuantG Core State Validator.

Inspects MongoDB collections and verifies that all expected indexes and structural fields are present.
Supports --dry-run and --apply modes.
"""
import os
import sys
import argparse
from pymongo import MongoClient

def main():
    parser = argparse.ArgumentParser(description="QuantG Core State Validator")
    parser.add_argument("--dry-run", action="store_true", help="Perform checks only without making database mutations")
    parser.add_argument("--apply", action="store_true", help="Repair schemas and apply recommended indexes")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("Please specify either --dry-run or --apply")
        sys.exit(1)

    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    
    print(f"Connecting to MongoDB: {mongo_url} (DB: {db_name})")
    client = MongoClient(mongo_url)
    db = client[db_name]

    print("\n>>> STARTING QUANTG CORE DB SCHEMAS INSPECTION <<<\n")

    # 1. Check required collections
    required_cols = [
        "users", "strategies", "signals", "orders", "fills", "positions",
        "strategy_positions", "core_events", "portfolio_ledger",
        "strategy_performance", "backtest_runs", "live_arm_state", "risk_state"
    ]
    
    existing_cols = db.list_collection_names()
    print(f"Discovered collections: {existing_cols}")

    missing_cols = [col for col in required_cols if col not in existing_cols]
    
    if missing_cols:
        print(f"[!] Core collections missing: {missing_cols}")
        if args.apply:
            for col in missing_cols:
                db.create_collection(col)
                print(f"[+] Created collection: {col}")
        else:
            print("[*] Dry-run: Collections creation recommended.")
    else:
        print("[PASS] All core collections are present.")

    # 2. Check and Apply Core Indexes
    index_plans = {
        "orders": [
            ("idempotency_key", 1, {"unique": True, "sparse": True}),
            ("user_id", 1, {}),
            ("strategy_id", 1, {})
        ],
        "signals": [
            ("id", 1, {"unique": True}),
            ("status", 1, {}),
            ("strategy_id", 1, {})
        ],
        "strategy_positions": [
            ("id", 1, {"unique": True}),
            ("user_id", 1, {}),
            ("strategy_id", 1, {}),
            ("status", 1, {})
        ]
    }

    for col_name, indexes in index_plans.items():
        if col_name in db.list_collection_names():
            col = db[col_name]
            existing_indexes = [i["name"] for i in col.list_indexes()]
            
            for field, order, opts in indexes:
                idx_name = f"{field}_idx"
                if idx_name not in existing_indexes:
                    print(f"[!] Missing index: {idx_name} on {col_name}")
                    if args.apply:
                        try:
                            col.create_index([(field, order)], name=idx_name, **opts)
                            print(f"[+] Successfully applied index {idx_name} on {col_name}")
                        except Exception as e:
                            print(f"[!] Warning: Could not create index {idx_name} on {col_name} (possibly indexed under a different name): {e}")
                    else:
                        print(f"[*] Dry-run: Indexing recommended on {col_name}({field})")

    print("\n>>> DB VALIDATION COMPLETED <<<\n")

if __name__ == "__main__":
    main()
