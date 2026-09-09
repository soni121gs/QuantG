"""IMD-08: inspect current non-archived strategies with intraday OOS metrics.

Replays each intraday buyer over the stored 1-minute option history (IMD-03/05)
through the no-lookahead selector (IMD-06) + backtest engine (IMD-07), aggregates
the trades, and scores them with the sample-size-aware verdict (IMD-08 core). Like
the EOD validator, it is JUDGE-FIRST: until enough clean out-of-sample minute data
exists it returns INSUFFICIENT_DATA / DATA_QUALITY_FAIL rather than a flattering number.

    python scripts/run_intraday_options_validation.py \
        --from 2025-01-01 --to 2025-03-31

Underlying 1-minute index candles are the remaining data dependency (IMD-04 forward
capture / a future index-minute import). Without them the engine has nothing to
evaluate the signal on, so the run reports INSUFFICIENT_DATA — honestly.

Pure helpers (``compile_signal_fn``, ``build_scorecard``) are unit-tested; the CLI
glue needs the store + Mongo and is not.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.intraday_options_backtest import IntradayCosts, run_day
from core.intraday_options_oos import INSUFFICIENT_DATA, evaluate_strategy
from core.options_minute_store import OptionsMinuteStore

logger = logging.getLogger("quantg.intraday_oos")

def compile_signal_fn(python_code: str) -> Callable[[List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """Wrap a strategy's ``run(data)`` into a per-minute signal function.

    Strategy code returns a list (0 or 1 signal for ``data[-1]``); we surface the
    last emitted signal or None. Executed in an isolated namespace.
    """
    ns: Dict[str, Any] = {}
    exec(compile(python_code, "<strategy>", "exec"), ns)  # noqa: S102 - trusted in-repo template
    run = ns.get("run")
    if not callable(run):
        raise ValueError("strategy python_code has no run(data)")

    def signal_fn(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        try:
            out = run(history)
        except Exception:
            return None
        return out[-1] if isinstance(out, list) and out else None

    return signal_fn


def build_scorecard(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rank strategy verdicts most-promising first for the console/UI."""
    order = {"CANDIDATE_EDGE": 0, "FRAGILE": 1, "NO_EDGE_NEGATIVE": 2, "INSUFFICIENT_DATA": 3, "DATA_QUALITY_FAIL": 4}
    ranked = sorted(results, key=lambda r: (order.get(r["verdict"], 9), -r["overall"]["expectancy"]))
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict_counts": counts,
        "candidates": [r["strategy"] for r in results if r["verdict"] == "CANDIDATE_EDGE"],
        "results": ranked,
    }


def validate_strategy(
    name: str,
    python_code: str,
    *,
    underlying: str,
    structure: str,
    days: List[str],
    underlying_minutes_fn: Callable[[str, str], List[Dict[str, Any]]],
    chain_at_fn: Callable[[str, str], Callable[[str], Dict[str, Any]]],
    option_series_fn: Callable[[str, str], Dict[str, List[Dict[str, Any]]]],
    costs: Optional[IntradayCosts] = None,
) -> Dict[str, Any]:
    """Replay one strategy across ``days`` and score it. Empty data -> INSUFFICIENT_DATA."""
    raw_signal_fn = compile_signal_fn(python_code)

    def signal_fn(history):
        normalized = [{**bar, "date": bar.get("date") or bar.get("timestamp_ist")} for bar in history]
        signal = raw_signal_fn(normalized)
        return {**signal, "structure": structure} if signal else None

    all_trades: List[Dict[str, Any]] = []
    missing_days = 0
    for date in days:
        umin = underlying_minutes_fn(underlying, date)
        if not umin:
            missing_days += 1
            continue
        trades = run_day(
            underlying=underlying, date=date, underlying_minutes=umin,
            signal_fn=signal_fn, chain_at=chain_at_fn(underlying, date),
            option_series=option_series_fn(underlying, date), costs=costs,
        )
        all_trades.extend(t.__dict__ for t in trades)

    missing_rate = missing_days / len(days) if days else 1.0
    return evaluate_strategy(name, all_trades, missing_rate=missing_rate)


def _template_index() -> Dict[str, Dict[str, Any]]:
    from pymongo import MongoClient
    with MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017")) as client:
        rows = list(client[os.environ.get("DB_NAME", "quantg")].strategies.find(
            {"status": {"$ne": "archived"}, "python_code": {"$nin": [None, ""]}}))
    result = {}
    for row in rows:
        vc = row.get("visual_config") or {}
        options = vc.get("options") or {}
        result[row["id"]] = {**row, "underlying": options.get("underlying") or vc.get("symbol"),
                             "structure": options.get("structure") or "single_leg"}
    return result


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover - CLI glue
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Current-book intraday OOS coverage and validation")
    ap.add_argument("--strategies", default=None, help="Comma-separated current strategy IDs; default all non-archived rows")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--store-only", action="store_true", help="report coverage and exit")
    args = ap.parse_args(argv)

    from core.index_minute_store import IndexMinuteStore
    store = OptionsMinuteStore()
    index_store = IndexMinuteStore()
    source = "upstox"
    templates = _template_index()
    names = [s.strip() for s in args.strategies.split(",") if s.strip()] if args.strategies else list(templates)

    # Real providers: underlying minutes from the index store, chain + option
    # series from the options store. Empty where a day has no data -> the
    # validator counts it as missing and stays judge-first.
    def underlying_fn(u, d):
        return index_store.get_minutes(u, d)

    def chain_fn(u, d):
        return lambda ts: store.get_chain_at_time(source, u, d, ts)

    def series_fn(u, d):
        return store.all_series_for_day(source, u, d)

    no_underlying, empty_chain, empty_series = underlying_fn, chain_fn, series_fn

    days = sorted(set(store.trading_days() + index_store.trading_days()))
    days = [d for d in days if args.start <= d <= args.end]
    results = []
    for name in names:
        tpl = templates.get(name)
        if not tpl:
            results.append({"strategy": name, "verdict": INSUFFICIENT_DATA, "trades": 0,
                            "months": [], "overall": {"expectancy": 0.0}, "note": "template not found"})
            continue
        if (tpl.get("visual_config") or {}).get("risk", {}).get("exit_mode") == "hold_to_expiry":
            results.append({"strategy": name, "verdict": "DATA_QUALITY_FAIL", "trades": 0,
                            "months": [], "overall": {"expectancy": 0.0},
                            "note": "Single-day replay cannot validate the production multi-day exit policy"})
            continue
        results.append(validate_strategy(
            name, tpl.get("python_code", "def run(data):\n    return []"),
            underlying=str(tpl.get("underlying", "NIFTY")), structure=str(tpl.get("structure", "single_leg")),
            days=days, underlying_minutes_fn=no_underlying, chain_at_fn=empty_chain,
            option_series_fn=empty_series,
        ))

    scorecard = build_scorecard(results)
    scorecard["validation_scope"] = "current-book signal/structure replay; production exit-policy parity unverified"
    scorecard["promotion_eligible"] = False
    scorecard["candidates"] = []
    scorecard["strategy_configs"] = {name: {"python_code": tpl.get("python_code"),
                                            "visual_config": tpl.get("visual_config"),
                                            "geometry_changed_at": tpl.get("geometry_changed_at")}
                                     for name, tpl in templates.items() if name in names}
    print(f"Intraday OOS verdicts: {scorecard['verdict_counts']}")
    for r in scorecard["results"]:
        print(f"  {r['strategy']:>6}  {r['verdict']:<18} trades={r.get('trades', 0)}")

    try:
        from pymongo import MongoClient
        db = MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017")).quantg
        db.intraday_options_oos_runs.insert_one({**scorecard, "window": {"from": args.start, "to": args.end}})
    except Exception as err:
        logger.warning("could not persist run: %s", err)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
