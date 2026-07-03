#!/usr/bin/env python3
"""Edge-search sweep: run a grid of config variants through the EOD OOS validator
to find whether ANY configuration of the credit-spread book crosses into positive
out-of-sample territory — or prove none does.

The live geometry (take 50% of credit / stop at 100% of credit) is structurally
asymmetric: max loss = width-credit >> max win = credit, so it needs a high win
rate to be +EV. This sweep varies the exit geometry (credit_tp/credit_sl), the
spread width, and the expiry DTE to test if the asymmetry can be fixed.

Run inside the backend container (needs Mongo + the bhavcopy store copied to /app/data):
    docker exec quantg-backend python /app/scripts/run_edge_sweep.py
    docker exec quantg-backend python /app/scripts/run_edge_sweep.py --name "NIFTY Theta"
"""
import argparse
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymongo  # noqa: E402
from core.eod_options_backtest import EODOptionsBacktest, walk_forward  # noqa: E402
from core.bhavcopy_store import BhavcopyStore  # noqa: E402

# grid
CREDIT_TP = [0.35, 0.5, 0.7]     # fraction of credit captured to take profit
CREDIT_SL = [1.0, 2.0, 3.5]      # stop at this multiple of credit lost
WIDTH = [2, 4]                    # strikes wide
MAX_DTE = [4, 10]                 # near weekly vs ~2 weeks
MIN_DTE = 2

BASE_NAMES = ["NIFTY Theta Credit Spread", "BANKNIFTY Theta Credit Spread"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=None, help="substring; default = the theta credit spreads")
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"),
                             serverSelectionTimeoutMS=4000)[os.environ.get("DB_NAME", "quantg")]
    store = BhavcopyStore()
    engine = EODOptionsBacktest(store)

    names = [args.name] if args.name else BASE_NAMES
    strategies = []
    for n in names:
        s = next((x for x in db.strategies.find({"python_code": {"$nin": [None, ""]}})
                  if n.lower() in (x.get("name") or "").lower()), None)
        if s:
            strategies.append(s)
    if not strategies:
        print("no matching strategies")
        return

    grid = list(itertools.product(CREDIT_TP, CREDIT_SL, WIDTH, MAX_DTE))
    for strat in strategies:
        print(f"\n{'='*100}\nSWEEP: {strat['name']}  ({len(grid)} configs)\n{'='*100}")
        print(f"{'tp':>5}{'sl':>6}{'wid':>5}{'dte':>5}{'N':>6}{'TOT':>10}{'EXP':>8}{'WR%':>6}"
              f"{'OOS EXP':>9}{'GRN%':>6}  VERDICT")
        print("-" * 100)
        rows = []
        for tp, sl, width, dte in grid:
            params = {"credit_tp": tp, "credit_sl": sl, "width": width,
                      "min_dte": MIN_DTE, "max_dte": dte, "max_hold_days": dte}
            res = engine.run(strat, params=params)
            if res.get("error"):
                continue
            wf = walk_forward(res)
            o = wf["overall"]
            rows.append((tp, sl, width, dte, o, wf))
        # rank by OOS expectancy then overall expectancy
        rows.sort(key=lambda r: (-r[5]["oos"].get("expectancy", -9e9), -r[4]["expectancy"]))
        for tp, sl, width, dte, o, wf in rows[:args.top]:
            oos_exp = wf["oos"].get("expectancy", 0)
            print(f"{tp:>5}{sl:>6}{width:>5}{dte:>5}{o['n']:>6}{o['pnl']:>10.0f}{o['expectancy']:>8.0f}"
                  f"{o['win_rate']:>6.1f}{oos_exp:>9.0f}{wf['pct_green_months']:>6.0f}  {wf['verdict']}")
        edges = [r for r in rows if r[5]["verdict"] == "CANDIDATE_EDGE"]
        pos_oos = [r for r in rows if r[5]["oos"].get("expectancy", 0) > 0]
        print("-" * 100)
        print(f"  candidate-edge configs: {len(edges)} / {len(rows)}   |   "
              f"positive-OOS configs: {len(pos_oos)} / {len(rows)}")
        if edges:
            print("  >>> CANDIDATE EDGE FOUND — forward-paper before trusting:")
            for tp, sl, width, dte, o, wf in edges:
                print(f"      tp={tp} sl={sl} width={width} dte={dte}: "
                      f"exp=₹{o['expectancy']:.0f}/trade, OOS=₹{wf['oos']['expectancy']:.0f}, "
                      f"{wf['pct_green_months']:.0f}% green, n={o['n']}")


if __name__ == "__main__":
    main()
