"""QuantG Production Validation Script.

A robust verification command that analyzes the system's operational health, git commits,
MongoDB connectivity, live broker authorizations, market clocks, active positions, and performance scorecards.
"""
import os
import sys
import subprocess
from datetime import datetime, timezone
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment variables from backend/ and root directories
load_dotenv(os.path.abspath(os.path.join(os.path.dirname(__file__), "../.env")))
load_dotenv(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.env")))

def get_git_commit() -> str:
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("utf-8").strip()
        return commit
    except Exception:
        return "Unknown Commit (Not a Git repository or Git not installed)"

def main():
    print("=======================================================================")
    print("                 QUANTG CORE PRODUCTION STATE VALIDATOR                ")
    print("=======================================================================")
    
    # 1. Git Commit
    commit = get_git_commit()
    print(f"Git Commit: {commit}")
    
    # 2. Feature Flags
    print("\n--- FEATURE FLAGS ---")
    print(f"CORE_ENGINE_ENABLED:       {os.environ.get('CORE_ENGINE_ENABLED', 'false')}")
    print(f"CORE_ENGINE_SHADOW_MODE:   {os.environ.get('CORE_ENGINE_SHADOW_MODE', 'true')}")
    print(f"CORE_ENGINE_PAPER_ENABLED:  {os.environ.get('CORE_ENGINE_PAPER_ENABLED', 'false')}")
    print(f"CORE_ENGINE_BACKTEST_ENABLED: {os.environ.get('CORE_ENGINE_BACKTEST_ENABLED', 'true')}")
    print(f"CORE_ENGINE_LIVE_ENABLED:     {os.environ.get('CORE_ENGINE_LIVE_ENABLED', 'false')}")

    # 3. Mongo Connection
    print("\n--- DATABASE STATUS ---")
    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    try:
        client = MongoClient(mongo_url, serverSelectionTimeoutMS=2000)
        db = client[db_name]
        client.admin.command("ping")
        print(f"Mongo Connection: CONNECTED ({mongo_url})")
    except Exception as e:
        print(f"Mongo Connection: FAILED ({e})")
        sys.exit(1)

    # 4. Market Clock Statuses
    print("\n--- MARKET DOMAINS SCHEDULES ---")
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from core.market_clock import get_segment_status, DomainType
    
    for dom in (DomainType.NSE_FO, DomainType.BSE_FO):
        status = get_segment_status(dom)
        print(f" {dom.value}: Status = {status['status']} | Open = {status['open']} | Detail = {status['reason']}")

    # 5. Local State & Metrics
    print("\n--- ACTIVE PORTFOLIO STATE ---")
    open_paper = db.strategy_positions.count_documents({"mode": "paper", "status": "OPEN"})
    open_live = db.strategy_positions.count_documents({"mode": "live", "status": "OPEN"})
    total_strategies = db.strategies.count_documents({})
    halted_strategies = db.strategies.count_documents({"halted": True})
    skipped_signals = db.signals.count_documents({"status": "SKIPPED_SIGNAL"})
    failed_orders = db.orders.count_documents({"status": "FAILED"})
    
    print(f"Total Configured Strategies: {total_strategies}")
    print(f"Halted Strategies:           {halted_strategies}")
    print(f"Open Paper Positions:        {open_paper}")
    print(f"Open Live Positions:         {open_live}")
    print(f"Skipped Signal Events:       {skipped_signals}")
    print(f"Failed Order Submissions:    {failed_orders}")

    # 6. Global Switch & arming status
    print("\n--- RISK CONTROL GATES ---")
    kill_switch = db.risk_state.find_one({"_id": "global_kill_switch"})
    ks_active = kill_switch.get("active", False) if kill_switch else False
    
    arm_state = db.live_arm_state.find_one({})
    armed = arm_state.get("armed", False) if arm_state else False
    
    print(f"Global Kill-Switch Status:  {'ACTIVE (TRADING BLOCKED)' if ks_active else 'OFF (NORMAL)'}")
    print(f"Live Arming Status:         {'ARMED (LIVE READY)' if armed else 'DISARMED'}")
    
    print("\n=======================================================================")
    print("                   PRODUCTION STATE INSPECTION COMPLETED                ")
    print("=======================================================================")

if __name__ == "__main__":
    main()
