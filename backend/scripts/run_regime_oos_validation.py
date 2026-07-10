#!/usr/bin/env python3
"""RAE re-judge: grade EVERY option strategy (new / old / live / archived) through
the REFORMED regime-conditional judge (core/regime_conditional_oos.py, §18.2 #6).

The old scorecard (run_oos_validation.py) grades a BLENDED all-days number that
averages a seller's good RANGE days with its trend/chop bleed — the reason the
founder distrusts the verdict and the reason a real regime-specialist looks dead.
This script instead:

  1. Runs each strategy through the EOD bhavcopy backtester (settle-to-settle).
  2. Buckets every trade by its ENTRY-DAY regime, using the SAME no-lookahead RAE
     taxonomy the 498-day study used — classify_day() over that day's real index
     1-minute bars (data/index_1m).
  3. Picks the regime each strategy OWNS (credit/condor sellers -> RANGE;
     debit/long buyers -> TREND_UP) and grades it ON its regime, walk-forward,
     reporting OFF-regime give-back separately. Thin -> NEEDS_FORWARD_PAPER, never
     an auto-veto.

Covers ALL statuses so archived/paused rows are re-judged too, not just the live
book. Run inside the backend container (needs Mongo + data/):

    docker exec quantg-backend python /app/scripts/run_regime_oos_validation.py
    docker exec quantg-backend python /app/scripts/run_regime_oos_validation.py --status all --underlying NIFTY
    docker exec quantg-backend python /app/scripts/run_regime_oos_validation.py --name Theta
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # /app

import pymongo  # noqa: E402

from core.bhavcopy_store import BhavcopyStore  # noqa: E402
from core.eod_options_backtest import EODOptionsBacktest  # noqa: E402
from core.index_minute_store import IndexMinuteStore  # noqa: E402
from core import regime_taxonomy as rt  # noqa: E402
from core.regime_conditional_oos import evaluate_regime_conditional  # noqa: E402

VERDICT_TAG = {
    "POSITIVE_OOS": "✅ POSITIVE_OOS",
    "NEEDS_FORWARD_PAPER": "🟡 FWD_PAPER",
    "NO_EDGE": "❌ NO_EDGE",
    "INSUFFICIENT_DATA": "…  THIN",
}

# underlyings we can classify intraday (index_1m store); others -> no regime bucket
CLASSIFIABLE = {"NIFTY", "BANKNIFTY"}


def _target_regime(structure: str) -> str:
    """Regime the strategy OWNS under the RAE taxonomy (REGIME_OWNER)."""
    if structure in ("credit_spread", "iron_condor"):
        return rt.RANGE            # sellers own RANGE
    if structure in ("debit_spread", "single_leg"):
        return rt.TREND_UP        # buyers only pay in a trend
    return rt.RANGE


def build_regime_map(idx_store: IndexMinuteStore, underlying: str) -> dict:
    """{date -> RAE taxonomy label} from that day's real index 1-minute bars."""
    out = {}
    for day in idx_store.trading_days(underlying):
        bars = idx_store.get_minutes(underlying, day)
        if not bars:
            continue
        out[day] = rt.classify_day(bars)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--name", default=None, help="substring filter on strategy name")
    ap.add_argument("--status", default="all",
                    help="comma list of strategy statuses to include, or 'all' (default)")
    ap.add_argument("--underlying", default=None, help="only this underlying")
    ap.add_argument("--capital", type=float, default=100_000.0)
    args = ap.parse_args()

    mongo_url = os.environ.get("MONGO_URL", "mongodb://mongo:27017")
    db = pymongo.MongoClient(mongo_url, serverSelectionTimeoutMS=4000)[os.environ.get("DB_NAME", "quantg")]

    store = BhavcopyStore()
    days = store.trading_days()
    if not days:
        print("No bhavcopy data found. Run bhavcopy_ingest.py first.")
        return
    print(f"bhavcopy window: {days[0]} -> {days[-1]}  ({len(days)} trading days)")

    idx_store = IndexMinuteStore()
    regime_maps = {}
    for u in CLASSIFIABLE:
        m = build_regime_map(idx_store, u)
        if m:
            regime_maps[u] = m
            dist = {}
            for lbl in m.values():
                dist[lbl] = dist.get(lbl, 0) + 1
            print(f"index_1m {u}: {len(m)} classified days  {dist}")
    if not regime_maps:
        print("No index_1m data found -> cannot regime-bucket. Run index_1m_ingest_upstox.py.")
        return
    print()

    # load strategies (ALL statuses by default => new/old/live/archived all covered)
    q = {"python_code": {"$nin": [None, ""]}}
    if args.status and args.status.lower() != "all":
        wanted = [s.strip() for s in args.status.split(",") if s.strip()]
        q["status"] = {"$in": wanted}
    strategies = list(db.strategies.find(q))
    if args.name:
        strategies = [s for s in strategies if args.name.lower() in (s.get("name") or "").lower()]

    engine = EODOptionsBacktest(store)
    rows = []
    for s in strategies:
        name = s.get("name", "?")
        status = s.get("status", "?")
        res = engine.run(s, start=args.start, end=args.end, starting_capital=args.capital)
        structure = res.get("structure", "?")
        underlying = res.get("underlying", "?")
        if args.underlying and underlying != args.underlying:
            continue
        if res.get("error"):
            rows.append((name, status, structure, underlying, None, res["error"]))
            continue

        rmap = regime_maps.get(underlying)
        if not rmap:
            rows.append((name, status, structure, underlying, None,
                         f"no index_1m regime data for {underlying}"))
            continue

        target = _target_regime(structure)
        records = []
        for t in res.get("trades", []):
            d = str(t.get("entry_date"))[:10]
            lbl = rmap.get(d)
            if lbl is None:
                continue   # entry day outside classified window
            records.append({"date": d, "regime": lbl, "pnl": t.get("pnl")})
        verdict = evaluate_regime_conditional(records, target)
        rows.append((name, status, structure, underlying, target, verdict))

    order = {"POSITIVE_OOS": 0, "NEEDS_FORWARD_PAPER": 1, "INSUFFICIENT_DATA": 2, "NO_EDGE": 3}

    def sort_key(r):
        v = r[5]
        if not hasattr(v, "verdict"):
            return (9, 0.0)
        return (order.get(v.verdict, 8), -(v.on_regime.get("avg") or 0.0))

    rows.sort(key=sort_key)

    print(f"{'STRATEGY':<30}{'STATUS':<10}{'STRUCT':<13}{'OWNS':<11}"
          f"{'N@REG':>6}{'AVG':>8}{'WR%':>6}{'IS':>8}{'OOS':>8}{'OFF-N':>6}{'OFFAVG':>8}  VERDICT")
    print("-" * 140)
    for name, status, structure, underlying, target, v in rows:
        if not hasattr(v, "verdict"):
            print(f"{name[:29]:<30}{status[:9]:<10}{structure[:12]:<13}{'-':<11}"
                  f"{'-':>6}{'-':>8}{'-':>6}{'-':>8}{'-':>8}{'-':>6}{'-':>8}  ERR: {str(v)[:34]}")
            continue
        on = v.on_regime
        off = v.off_regime
        tag = VERDICT_TAG.get(v.verdict, v.verdict)
        print(f"{name[:29]:<30}{status[:9]:<10}{structure[:12]:<13}{target[:10]:<11}"
              f"{on.get('n', 0):>6}{on.get('avg', 0):>8.1f}{on.get('wr', 0):>6.1f}"
              f"{v.in_sample.get('avg', 0):>8.1f}{v.out_sample.get('avg', 0):>8.1f}"
              f"{off.get('n', 0):>6}{off.get('avg', 0):>8.1f}  {tag}")

    print("-" * 140)
    graded = [r for r in rows if hasattr(r[5], "verdict")]
    pos = [r for r in graded if r[5].verdict == "POSITIVE_OOS"]
    fwd = [r for r in graded if r[5].verdict == "NEEDS_FORWARD_PAPER"]
    print(f"\n{len(graded)} strategies re-judged on their owned regime | "
          f"{len(pos)} POSITIVE_OOS, {len(fwd)} NEEDS_FORWARD_PAPER, "
          f"{len(graded) - len(pos) - len(fwd)} NO_EDGE/THIN")
    print("Legend: N@REG/AVG/WR = trades & avg ₹ & win% ON the owned regime's days; "
          "IS/OOS = walk-forward avg on regime days; OFF = give-back on off-regime days.")
    if pos:
        print("\nPOSITIVE on owned regime (forward-paper before scaling):")
        for name, status, structure, underlying, target, v in pos:
            print(f"  • {name} [{underlying} {structure}, owns {target}] "
                  f"on-regime avg ₹{v.on_regime['avg']}/tr n={v.on_regime['n']}, "
                  f"IS {v.in_sample.get('avg')}/OOS {v.out_sample.get('avg')}")


if __name__ == "__main__":
    main()
