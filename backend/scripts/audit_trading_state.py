import os
import sys
from datetime import datetime, timezone
import pymongo

def main():
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    
    print("=" * 60)
    print("          QUANTG TRADING STATE AUDIT REPORT")
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
        
    # 1. Orders Summary
    total_orders = db.orders.count_documents({})
    paper_orders = db.orders.count_documents({"mode": "paper"})
    live_orders = db.orders.count_documents({"mode": "live"})
    filled_orders = db.orders.count_documents({"status": "FILLED"})
    skipped_orders = db.orders.count_documents({"status": "SKIPPED"})
    failed_orders = db.orders.count_documents({"status": "FAILED"})
    review_orders = db.orders.count_documents({"status": "UNKNOWN_NEEDS_REVIEW"})
    
    print("1. ORDERS METRICS:")
    print(f"   - Total Orders in DB: {total_orders}")
    print(f"   - Paper Orders: {paper_orders} | Live Orders: {live_orders}")
    print(f"   - Fills: {filled_orders} | Skips: {skipped_orders} | Fails: {failed_orders}")
    print(f"   - Needing Manual Review: {review_orders}")
    print("-" * 60)
    
    # 2. Active Positions
    active_positions = list(db.strategy_positions.find({"status": "OPEN"}))
    print("2. OPEN POSITIONS:")
    if not active_positions:
        print("   - No active open positions.")
    for idx, pos in enumerate(active_positions, 1):
        print(f"   {idx}. {pos.get('symbol')} ({pos.get('mode')}) | Side: {pos.get('side')} | Qty: {pos.get('qty')} | Entry: Rs. {pos.get('price')} | Strategy: {pos.get('strategy_id')}")
    print("-" * 60)
    
    # 3. Skipped Signals Summary
    skipped_signals = list(db.skipped_signals.find({}).sort("last_seen_at", -1).limit(10))
    print("3. RECENT SKIPPED SIGNALS (Max 10):")
    if not skipped_signals:
        print("   - No skipped signals recorded.")
    for idx, sig in enumerate(skipped_signals, 1):
        print(f"   {idx}. {sig.get('symbol')} | Reason Code: {sig.get('reason_code')} | Reason: {sig.get('skip_reason')} | Count: {sig.get('count')}")
    print("-" * 60)
    
    # 4. Risk state
    kill_switch = db.risk_state.find_one({"_id": "global_kill_switch"})
    kill_active = kill_switch.get("active") if kill_switch else False
    recon_status = db.risk_state.find_one({"_id": "position_reconciliation"})
    mismatch_detected = recon_status.get("mismatch_detected") if recon_status else False
    
    print("4. RISK ENGINE STATUS:")
    print(f"   - Global Kill-Switch Active: {kill_active}")
    print(f"   - Position Reconciliation Mismatch Detected: {mismatch_detected}")
    print("-" * 60)
    
    # 5. Live Arm State
    arm_state = list(db.live_arm_state.find({}))
    print("5. LIVE ARM STATES:")
    if not arm_state:
        print("   - No live arm records in database.")
    for idx, arm in enumerate(arm_state, 1):
        print(f"   {idx}. User ID: {arm.get('user_id')} | Armed: {arm.get('armed')} | Global Live Enabled: {arm.get('global_live_enabled')}")
    print("-" * 60)

if __name__ == "__main__":
    main()
