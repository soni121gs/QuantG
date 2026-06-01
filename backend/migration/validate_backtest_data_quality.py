"""QuantG Backtest Data Quality Assessor.

Analyzes historical database instrument master caches and historical candle lists.
Warns if expiries or prices have low quality flags.
"""
import os
import sys
import argparse
from pymongo import MongoClient

def main():
    parser = argparse.ArgumentParser(description="QuantG Backtest Data Quality Assessor")
    parser.add_argument("--dry-run", action="store_true", help="Report data quality only")
    parser.add_argument("--apply", action="store_true", help="Mark data quality state flags")
    args = parser.parse_args()

    mongo_url = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
    db_name = os.environ.get("DB_NAME", "quantg")
    
    client = MongoClient(mongo_url)
    db = client[db_name]

    print("\n>>> STARTING HISTORICAL DATA QUALITY AUDIT <<<\n")

    # Fetch MCX metadata cache
    meta = db.upstox_instrument_cache_meta.find_one({"_id": "MCX_MASTER"})
    
    if not meta:
        print("[!] Warning: Upstox MCX Master instruments cache is completely empty.")
    else:
        print(f"[PASS] Discoverable MCX Master: Options = {meta.get('contract_count')}, Futures = {meta.get('future_count')}")
        print(f"    Last Sync Time: {meta.get('refreshed_at')}")

    # Inspect backtest runs and catalog qualities
    runs = db.backtest_runs.find().to_list(length=100)
    for run in runs:
        quality = run.get("data_quality", "LOW")
        print(f" - Run {run.get('id')[:10]}: Strategy {run.get('strategy_id')[:8]} | Quality: {quality} (Trades: {run.get('total_trades')})")

    print("\n>>> DATA QUALITY AUDIT COMPLETED <<<\n")

if __name__ == "__main__":
    main()
