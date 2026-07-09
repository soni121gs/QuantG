"""EM-7 walk-forward comparison of dynamic EdgeMath sizing versus flat sizing."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List

from core.edge_sizer import edge_size, payoff_ratio, rolling_stats_from_pnls


def _metrics(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    daily = defaultdict(float)
    for row in rows:
        daily[str(row["date"])[:10]] += float(row["pnl"])
    vals = list(daily.values())
    equity = peak = max_dd = 0.0
    for pnl in vals:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    losses = abs(sum(v for v in vals if v < 0))
    profits = sum(v for v in vals if v > 0)
    return {
        "net_pnl": round(sum(vals), 2),
        "expectancy": round(sum(vals) / len(rows), 2) if rows else 0.0,
        "max_daily_drawdown": round(max_dd, 2),
        "loss_profit_ratio": round(losses / profits, 3) if profits else float("inf"),
        "green_day_pct": round(100 * sum(v > 0 for v in vals) / len(vals), 1) if vals else 0.0,
    }


def compare_dynamic_to_flat(
    trades: List[Dict[str, Any]],
    *,
    starting_equity: float = 500_000.0,
    per_lot_max_loss: float = 5_000.0,
    daily_risk_budget: float = 10_000.0,
    warmup: int = 10,
) -> Dict[str, Any]:
    """No-lookahead replay: each decision sees only earlier closed trades."""
    history: List[float] = []
    day_pnl = peak_day_pnl = 0.0
    current_day = None
    flat_rows, dynamic_rows = [], []
    for trade in sorted(trades, key=lambda r: (str(r.get("date") or r.get("exit_day")), str(r.get("exit_time") or ""))):
        date = str(trade.get("date") or trade.get("exit_day"))[:10]
        if date != current_day:
            current_day, day_pnl, peak_day_pnl = date, 0.0, 0.0
        unit_pnl = float(trade.get("pnl") or trade.get("net_pnl") or 0.0)
        stats = rolling_stats_from_pnls(history[-60:])
        decision = edge_size(
            stats=stats, payoff_b=payoff_ratio(stats.avg_win, stats.avg_loss),
            equity=max(1.0, starting_equity + sum(r["pnl"] for r in dynamic_rows)),
            per_lot_max_loss=per_lot_max_loss, day_pnl=day_pnl,
            daily_risk_budget=daily_risk_budget, peak_day_pnl=peak_day_pnl,
            min_trades=warmup, floor_lots=0,
        )
        lots = decision.lots
        sized_pnl = unit_pnl * lots
        flat_rows.append({"date": date, "pnl": unit_pnl})
        dynamic_rows.append({"date": date, "pnl": sized_pnl, "lots": lots, "edge_score": decision.edge_score})
        day_pnl += sized_pnl
        peak_day_pnl = max(peak_day_pnl, day_pnl)
        history.append(unit_pnl)
    flat, dynamic = _metrics(flat_rows), _metrics(dynamic_rows)
    passed = (
        dynamic["expectancy"] > flat["expectancy"]
        and dynamic["max_daily_drawdown"] < flat["max_daily_drawdown"]
        and dynamic["loss_profit_ratio"] < flat["loss_profit_ratio"]
    )
    return {"passed": passed, "verdict": "PASS" if passed else "FAIL", "flat": flat, "dynamic": dynamic,
            "trades": len(trades), "warmup": warmup}
