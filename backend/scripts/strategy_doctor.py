import os
import sys
from datetime import datetime, timezone
import pymongo

def main():
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    
    print("=" * 60)
    print("         QUANTG STRATEGY DOCTOR CLI DIAGNOSTIC")
    print("=" * 60)
    print(f"Connecting to: {mongo_url}")
    print(f"Database Name: {db_name}")
    print("-" * 60)
    
    try:
        client = pymongo.MongoClient(mongo_url, serverSelectionTimeoutMS=2000)
        client.admin.command('ping')
        db = client[db_name]
    except Exception as e:
        print(f"Error connecting to MongoDB: {e}")
        sys.exit(1)
        
    strategies = list(db.strategies.find({}))
    if not strategies:
        print("No strategies found in the database.")
        sys.exit(0)
        
    print(f"Found {len(strategies)} total strategies.\n")
    
    for idx, strat in enumerate(strategies, 1):
        sid = strat.get("id") or str(strat.get("_id"))
        name = strat.get("name", "Unnamed")
        status = strat.get("status", "inactive")
        mode = strat.get("mode", "paper")
        symbol = strat.get("symbol", "N/A")
        segment = strat.get("instrument_group") or "N/A"
        is_halted = strat.get("halted") or strat.get("is_halted") or False
        halt_reason = strat.get("halt_reason") or strat.get("last_halt_reason") or "None"
        
        last_eval = strat.get("last_evaluated_at")
        last_signal = strat.get("last_signal_action") or "None"
        last_skip_reason = strat.get("last_filter_reason") or strat.get("last_error") or "None"
        last_skip_code = strat.get("last_skip_reason_code") or "N/A"
        
        skipped_today = strat.get("skipped_count_today", 0)
        
        # Check active position for this strategy
        pos = db.strategy_positions.find_one({
            "strategy_id": sid,
            "status": "OPEN"
        })
        has_pos = "OPEN" if pos else "CLOSED"
        pos_details = f"{pos.get('symbol')} @ Rs. {pos.get('price')} (Qty: {pos.get('qty')})" if pos else "None"
        
        print(f"{idx}. Strategy: {name} (ID: {sid})")
        print(f"   - Status: {status.upper()} | Mode: {mode.upper()} | Segment: {segment}")
        print(f"   - Target Symbol: {symbol}")
        print(f"   - Halted: {is_halted} (Reason: {halt_reason})")
        print(f"   - Last Evaluated: {last_eval}")
        print(f"   - Last Signal: {last_signal}")
        print(f"   - Last Skip/Halt Code: {last_skip_code} (Reason: {last_skip_reason})")
        print(f"   - Skips Today: {skipped_today}")
        print(f"   - Position Status: {has_pos} (Details: {pos_details})")
        print("-" * 60)

if __name__ == "__main__":
    main()
