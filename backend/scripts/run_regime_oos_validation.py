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


_ORDER = {"POSITIVE_OOS": 0, "NEEDS_FORWARD_PAPER": 1, "INSUFFICIENT_DATA": 2, "NO_EDGE": 3}


def compute(db, *, start=None, end=None, name=None, status="all",
            underlying=None, capital=100_000.0):
    """Run the whole re-judge and return a JSON-safe result dict:
        {window, regime_distribution, rows:[...], summary:{...}}
    Each row: {name,status,structure,underlying,owns,verdict, on_regime, in_sample,
               out_sample, off_regime, stood_down_days, reasons, error}.
    Shared by the CLI (main) and the /ops/regime-oos route."""
    store = BhavcopyStore()
    days = store.trading_days()
    if not days:
        return {"error": "bhavcopy store empty — run bhavcopy_ingest.py first"}

    idx_store = IndexMinuteStore()
    regime_maps = {}
    regime_distribution = {}
    for u in CLASSIFIABLE:
        m = build_regime_map(idx_store, u)
        if m:
            regime_maps[u] = m
            dist = {}
            for lbl in m.values():
                dist[lbl] = dist.get(lbl, 0) + 1
            regime_distribution[u] = {"days": len(m), "labels": dist}
    if not regime_maps:
        return {"error": "no index_1m data — run index_1m_ingest_upstox.py"}

    q = {"python_code": {"$nin": [None, ""]}}
    if status and str(status).lower() != "all":
        wanted = [s.strip() for s in str(status).split(",") if s.strip()]
        q["status"] = {"$in": wanted}
    strategies = list(db.strategies.find(q))
    if name:
        strategies = [s for s in strategies if name.lower() in (s.get("name") or "").lower()]

    engine = EODOptionsBacktest(store)
    rows = []
    for s in strategies:
        nm = s.get("name", "?")
        st = s.get("status", "?")
        res = engine.run(s, start=start, end=end, starting_capital=capital)
        structure = res.get("structure", "?")
        u = res.get("underlying", "?")
        if underlying and u != underlying:
            continue
        base = {"name": nm, "status": st, "structure": structure, "underlying": u,
                "owns": None, "verdict": None, "error": None,
                "on_regime": {}, "in_sample": {}, "out_sample": {},
                "off_regime": {}, "stood_down_days": 0, "reasons": []}
        if res.get("error"):
            base["error"] = res["error"]
            rows.append(base)
            continue
        rmap = regime_maps.get(u)
        if not rmap:
            base["error"] = f"no index_1m regime data for {u}"
            rows.append(base)
            continue
        opt = (s.get("visual_config") or {}).get("options") or {}
        owned = opt.get("owned_regimes")
        if owned:
            # multi-regime ownership (e.g. trend_delta1 owns TREND_UP+TREND_DOWN):
            # collapse any owned label to "OWNED" so the judge grades their union.
            owned_set = set(owned)
            target, owns_label = "OWNED", "+".join(owned)
        else:
            owned_set, target = None, _target_regime(structure)
            owns_label = target
        records = []
        for t in res.get("trades", []):
            d = str(t.get("entry_date"))[:10]
            lbl = rmap.get(d)
            if lbl is None:
                continue
            if owned_set is not None:
                lbl = "OWNED" if lbl in owned_set else lbl
            records.append({"date": d, "regime": lbl, "pnl": t.get("pnl")})
        v = evaluate_regime_conditional(records, target)
        base.update({"owns": owns_label, "verdict": v.verdict, "on_regime": v.on_regime,
                     "in_sample": v.in_sample, "out_sample": v.out_sample,
                     "off_regime": v.off_regime, "stood_down_days": v.stood_down_days,
                     "reasons": v.reasons})
        rows.append(base)

    rows.sort(key=lambda r: (_ORDER.get(r["verdict"], 9) if r["verdict"] else 9,
                             -((r["on_regime"] or {}).get("avg") or 0.0)))
    graded = [r for r in rows if r["verdict"]]
    summary = {
        "total": len(rows), "graded": len(graded),
        "positive_oos": sum(1 for r in graded if r["verdict"] == "POSITIVE_OOS"),
        "needs_forward_paper": sum(1 for r in graded if r["verdict"] == "NEEDS_FORWARD_PAPER"),
        "no_edge_or_thin": sum(1 for r in graded if r["verdict"] in ("NO_EDGE", "INSUFFICIENT_DATA")),
        "not_backtestable": len(rows) - len(graded),
    }
    return {"kind": "regime_conditional_oos",
            "window": {"start": days[0], "end": days[-1], "trading_days": len(days)},
            "regime_distribution": regime_distribution,
            "rows": rows, "summary": summary}


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

    result = compute(db, start=args.start, end=args.end, name=args.name,
                     status=args.status, underlying=args.underlying, capital=args.capital)
    if result.get("error"):
        print(result["error"])
        return
    w = result["window"]
    print(f"bhavcopy window: {w['start']} -> {w['end']}  ({w['trading_days']} trading days)")
    for u, d in result["regime_distribution"].items():
        print(f"index_1m {u}: {d['days']} classified days  {d['labels']}")
    print()

    print(f"{'STRATEGY':<30}{'STATUS':<10}{'STRUCT':<13}{'OWNS':<11}"
          f"{'N@REG':>6}{'AVG':>8}{'WR%':>6}{'IS':>8}{'OOS':>8}{'OFF-N':>6}{'OFFAVG':>8}  VERDICT")
    print("-" * 140)
    for r in result["rows"]:
        if r["error"]:
            print(f"{r['name'][:29]:<30}{r['status'][:9]:<10}{r['structure'][:12]:<13}{'-':<11}"
                  f"{'-':>6}{'-':>8}{'-':>6}{'-':>8}{'-':>8}{'-':>6}{'-':>8}  ERR: {r['error'][:34]}")
            continue
        on = r["on_regime"] or {}
        off = r["off_regime"] or {}
        tag = VERDICT_TAG.get(r["verdict"], r["verdict"])
        print(f"{r['name'][:29]:<30}{r['status'][:9]:<10}{r['structure'][:12]:<13}{(r['owns'] or '')[:10]:<11}"
              f"{on.get('n', 0):>6}{on.get('avg', 0):>8.1f}{on.get('wr', 0):>6.1f}"
              f"{r['in_sample'].get('avg', 0):>8.1f}{r['out_sample'].get('avg', 0):>8.1f}"
              f"{off.get('n', 0):>6}{off.get('avg', 0):>8.1f}  {tag}")

    print("-" * 140)
    s = result["summary"]
    print(f"\n{s['graded']} strategies re-judged on their owned regime | "
          f"{s['positive_oos']} POSITIVE_OOS, {s['needs_forward_paper']} NEEDS_FORWARD_PAPER, "
          f"{s['no_edge_or_thin']} NO_EDGE/THIN")
    print("Legend: N@REG/AVG/WR = trades & avg ₹ & win% ON the owned regime's days; "
          "IS/OOS = walk-forward avg on regime days; OFF = give-back on off-regime days.")
    pos = [r for r in result["rows"] if r["verdict"] == "POSITIVE_OOS"]
    if pos:
        print("\nPOSITIVE on owned regime (forward-paper before scaling):")
        for r in pos:
            print(f"  • {r['name']} [{r['underlying']} {r['structure']}, owns {r['owns']}] "
                  f"on-regime avg ₹{r['on_regime']['avg']}/tr n={r['on_regime']['n']}, "
                  f"IS {r['in_sample'].get('avg')}/OOS {r['out_sample'].get('avg')}")


if __name__ == "__main__":
    main()
