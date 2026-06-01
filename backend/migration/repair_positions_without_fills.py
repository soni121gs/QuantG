"""QuantG Portfolio Integrity Position Healer.

Enforces: No fill = no position.
Scans strategy_positions and confirms matching fill records exist.
Removes or closes positions that have zero verified fill records.
Supports --dry-run and --apply.
"""
import os
import sys
import argparse
from pymongo import MongoClient

def main():
    parser = argparse.ArgumentParser(description="QuantG Position Healer")
    parser.add_argument("--dry-run", action="store_true", help="Audit position and fill mismatches only")
    parser.add_argument("--apply", action="store_true", help="Repair and close un-filled positions")
    args = parser.parse_args()

    if not args.dry_run and not args.apply:
        print("Please specify either --dry-run or --apply")
        sys.exit(1)

    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    
    client = MongoClient(mongo_url)
    db = client[db_name]

    print("\n>>> STARTING POSITION AND FILL AUDIT HEALER <<<\n")

    # Find open strategy positions
    open_positions = db.strategy_positions.find({"status": "OPEN"})
    position_count = 0
    healed_count = 0

    for pos in open_positions:
        position_count += 1
        pos_id = pos["id"]
        strat_id = pos["strategy_id"]
        sym = pos["target_symbol"]
        
        # Look for matching fills
        fill = db.fills.find_one({
            "strategy_id": strat_id,
            "target_symbol": sym,
            "mode": pos["mode"]
        })
        
        if not fill:
            print(f"[!] Mismatch detected: Position {pos_id} for strategy {strat_id} ({sym}) has no matching fill record.")
            if args.apply:
                db.strategy_positions.update_one(
                    {"_id": pos["_id"]},
                    {"$set": {
                        "status": "CLOSED",
                        "open_quantity": 0,
                        "exit_reason": "healer-closed-missing-fill",
                        "updated_at": pos.get("updated_at")
                    }}
                )
                
                # Delete portfolio hold if matching
                db.positions.delete_one({
                    "user_id": pos["user_id"],
                    "symbol": sym,
                    "mode": pos["mode"]
                })
                healed_count += 1
            else:
                print(f"[*] Dry-run: Document {pos_id} needs closure/deletion.")

    print(f"\nAudit completed. Active positions inspected: {position_count}. Repaired/Healed: {healed_count}.")
    print("\n>>> POSITION INTEGRITY COMPLETED <<<\n")

if __name__ == "__main__":
    main()
