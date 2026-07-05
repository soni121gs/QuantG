"""IMD-08 (core): sample-size-aware OOS metrics + verdict for intraday buyers.

Pure logic — takes the trade rows the intraday backtester (IMD-07) produced across
many days and returns an honest, judge-first verdict. Mirrors the EOD validator's
discipline (core/eod_options_backtest.walk_forward): the OOS test is TEMPORAL (hold
out the latest months), there is no parameter fitting here, and a strategy cannot
pass on a thin sample or on poor data coverage.

Verdicts: CANDIDATE_EDGE / FRAGILE / NO_EDGE_NEGATIVE / INSUFFICIENT_DATA /
DATA_QUALITY_FAIL. The gate thresholds are the promotion law referenced by IMD-10.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

# promotion gate (IMD-10 references these)
GATE = {
    "min_trades": 30,
    "min_months": 3,
    "max_missing_rate": 0.20,
    "min_oos_expectancy": 0.0,
    "min_green_month_pct": 0.5,
    "oos_months": 1,
}

CANDIDATE_EDGE = "CANDIDATE_EDGE"
FRAGILE = "FRAGILE"
NO_EDGE_NEGATIVE = "NO_EDGE_NEGATIVE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
DATA_QUALITY_FAIL = "DATA_QUALITY_FAIL"


def _month(date: str) -> str:
    return str(date)[:7]


def _hold_minutes(entry_ts: str, exit_ts: str) -> float:
    try:
        a = datetime.fromisoformat(str(entry_ts).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(exit_ts).replace("Z", "+00:00"))
        return max(0.0, (b - a).total_seconds() / 60.0)
    except (TypeError, ValueError):
        return 0.0


def _drawdown(pnls: List[float]) -> float:
    peak = equity = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return round(max_dd, 2)


def metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not trades:
        return {"trades": 0, "net_pnl": 0.0, "expectancy": 0.0, "win_rate": 0.0,
                "profit_factor": 0.0, "max_drawdown": 0.0, "avg_mfe": 0.0, "avg_mae": 0.0,
                "avg_hold_min": 0.0}
    pnls = [float(t.get("net_pnl", 0.0)) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    n = len(trades)
    return {
        "trades": n,
        "net_pnl": round(sum(pnls), 2),
        "expectancy": round(sum(pnls) / n, 2),
        "win_rate": round(len(wins) / n, 4),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else (float("inf") if gross_win else 0.0),
        "max_drawdown": _drawdown(pnls),
        "avg_mfe": round(sum(float(t.get("mfe", 0.0)) for t in trades) / n, 4),
        "avg_mae": round(sum(float(t.get("mae", 0.0)) for t in trades) / n, 4),
        "avg_hold_min": round(sum(_hold_minutes(t.get("entry_ts", ""), t.get("exit_ts", "")) for t in trades) / n, 2),
    }


def _green_month_pct(trades: List[Dict[str, Any]]) -> float:
    by_month: Dict[str, float] = {}
    for t in trades:
        by_month[_month(t.get("date", ""))] = by_month.get(_month(t.get("date", "")), 0.0) + float(t.get("net_pnl", 0.0))
    if not by_month:
        return 0.0
    green = sum(1 for v in by_month.values() if v > 0)
    return round(green / len(by_month), 4)


def walk_forward(trades: List[Dict[str, Any]], oos_months: int = 1) -> Dict[str, Any]:
    """Hold out the latest ``oos_months`` calendar months as out-of-sample."""
    months = sorted({_month(t.get("date", "")) for t in trades})
    oos_set = set(months[-oos_months:]) if len(months) > oos_months else set(months)
    train = [t for t in trades if _month(t.get("date", "")) not in oos_set]
    oos = [t for t in trades if _month(t.get("date", "")) in oos_set]
    return {
        "months": months,
        "oos_months": sorted(oos_set),
        "overall": metrics(trades),
        "train": metrics(train),
        "oos": metrics(oos),
        "green_month_pct": _green_month_pct(trades),
    }


def evaluate_strategy(
    name: str,
    trades: List[Dict[str, Any]],
    *,
    missing_rate: float = 0.0,
    skipped_signals: int = 0,
    gate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    g = {**GATE, **(gate or {})}
    n = len(trades)
    months = sorted({_month(t.get("date", "")) for t in trades})
    wf = walk_forward(trades, g["oos_months"])

    if missing_rate > g["max_missing_rate"]:
        verdict = DATA_QUALITY_FAIL
    elif n < g["min_trades"] or len(months) < g["min_months"]:
        verdict = INSUFFICIENT_DATA
    elif wf["overall"]["expectancy"] <= 0:
        verdict = NO_EDGE_NEGATIVE
    elif wf["oos"]["expectancy"] > g["min_oos_expectancy"] and wf["green_month_pct"] >= g["min_green_month_pct"]:
        verdict = CANDIDATE_EDGE
    else:
        verdict = FRAGILE

    return {
        "strategy": name,
        "verdict": verdict,
        "trades": n,
        "months": months,
        "missing_rate": round(missing_rate, 4),
        "skipped_signals": skipped_signals,
        "overall": wf["overall"],
        "train": wf["train"],
        "oos": wf["oos"],
        "green_month_pct": wf["green_month_pct"],
        "gate": g,
    }
