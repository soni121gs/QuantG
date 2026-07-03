#!/usr/bin/env python3
"""Out-of-sample validation scorecard for the option book.

Runs every option strategy through the EOD bhavcopy backtester over the full
history and prints an honest, walk-forward verdict per strategy: does it make
money OUT-OF-SAMPLE (the latest year) and consistently across months, or is it
noise to delete?

Run inside the backend container so it sees Mongo + the app modules:
    docker exec quantg-backend python /app/scripts/run_oos_validation.py
    docker exec quantg-backend python /app/scripts/run_oos_validation.py --start 2024-01-01 --end 2025-12-31
    docker exec quantg-backend python /app/scripts/run_oos_validation.py --name "SENSEX Theta"
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /app

import pymongo  # noqa: E402
from core.eod_options_backtest import EODOptionsBacktest, walk_forward  # noqa: E402
from core.bhavcopy_store import BhavcopyStore  # noqa: E402

VERDICT_TAG = {
    "CANDIDATE_EDGE": "✅ EDGE",
    "FRAGILE": "🟡 FRAGILE",
    "NO_EDGE_NEGATIVE": "❌ NO EDGE",
    "INSUFFICIENT_DATA": "…  THIN",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--name", default=None, help="substring filter on strategy name")
    ap.add_argument("--capital", type=float, default=100_000.0)
    args = ap.parse_args()

    mongo_url = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
    db = pymongo.MongoClient(mongo_url, serverSelectionTimeoutMS=4000)[os.environ.get("DB_NAME", "quantg")]

    store = BhavcopyStore()
    days = store.trading_days()
    if not days:
        print("No bhavcopy data found. Run bhavcopy_ingest.py first.")
        return
    print(f"bhavcopy window: {days[0]} -> {days[-1]}  ({len(days)} trading days)\n")

    q = {"python_code": {"$nin": [None, ""]}}
    strategies = list(db.strategies.find(q))
    if args.name:
        strategies = [s for s in strategies if args.name.lower() in (s.get("name") or "").lower()]

    engine = EODOptionsBacktest(store)
    rows = []
    for s in strategies:
        res = engine.run(s, start=args.start, end=args.end, starting_capital=args.capital)
        if res.get("error"):
            rows.append((s.get("name", "?"), None, res["error"]))
            continue
        wf = walk_forward(res)
        rows.append((s.get("name", "?"), res, wf))

    # sort: edges first, then by OOS expectancy
    def sort_key(r):
        _, res, wf = r
        if not isinstance(wf, dict):
            return (9, 0)
        order = {"CANDIDATE_EDGE": 0, "FRAGILE": 1, "INSUFFICIENT_DATA": 2, "NO_EDGE_NEGATIVE": 3}
        return (order.get(wf["verdict"], 8), -wf["overall"]["expectancy"])
    rows.sort(key=sort_key)

    print(f"{'STRATEGY':<34}{'STRUCT':<14}{'N':>5}{'TOT P&L':>10}{'EXP':>8}{'WR%':>6}{'OOS EXP':>9}{'GRN%':>6}  VERDICT")
    print("-" * 118)
    for name, res, wf in rows:
        if not isinstance(wf, dict):
            print(f"{name[:33]:<34}{'-':<14}{'-':>5}{'-':>10}{'-':>8}{'-':>6}{'-':>9}{'-':>6}  {str(wf)[:30]}")
            continue
        o = wf["overall"]
        oos_exp = wf.get("oos", {}).get("expectancy", 0)
        tag = VERDICT_TAG.get(wf["verdict"], wf["verdict"])
        print(f"{name[:33]:<34}{res['structure']:<14}{o['n']:>5}{o['pnl']:>10.0f}"
              f"{o['expectancy']:>8.1f}{o['win_rate']:>6.1f}{oos_exp:>9.1f}"
              f"{wf['pct_green_months']:>6.0f}  {tag}")

    edges = [r for r in rows if isinstance(r[2], dict) and r[2]["verdict"] == "CANDIDATE_EDGE"]
    print("-" * 118)
    print(f"\n{len(edges)} candidate-edge strateg{'y' if len(edges)==1 else 'ies'} out of "
          f"{sum(1 for r in rows if isinstance(r[2], dict))} backtestable.")
    if edges:
        print("Candidate edges (validate further before scaling capital):")
        for name, res, wf in edges:
            print(f"  • {name}  [{res['underlying']} {res['structure']}]  "
                  f"OOS {wf['oos_year']} exp=₹{wf['oos']['expectancy']}/trade, "
                  f"{wf['pct_green_months']:.0f}% green months")


if __name__ == "__main__":
    main()
