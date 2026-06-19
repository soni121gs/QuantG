"""Pure performance / risk-adjusted metrics.

No DB, no I/O — fully unit-testable. This is the single source of truth for
Sharpe, Sortino, max-drawdown, profit-factor, expectancy, etc. Both the options
backtester (core/options_backtest.py) and the live-trade scorecard
(core/strategy_scorecard.py) compute from here, so a strategy's backtest score
and its realized score are directly comparable.

Convention: a "trade" is one realized round-trip P&L in account currency (INR).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

TRADING_SESSIONS_PER_YEAR = 252.0


def _empty() -> Dict[str, Any]:
    return {
        "total_trades": 0, "total_pnl": 0.0, "return_pct": 0.0,
        "wins": 0, "losses": 0, "win_rate": 0.0,
        "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "payoff_ratio": 0.0, "expectancy": 0.0,
        "max_drawdown_pct": 0.0, "sharpe": 0.0, "sortino": 0.0,
        "equity_curve": [], "best_trade": 0.0, "worst_trade": 0.0,
    }


def compute_metrics(
    pnls: List[float],
    starting_capital: float = 100_000.0,
    returns_basis: Optional[float] = None,
) -> Dict[str, Any]:
    """Compute a full risk-adjusted scorecard from an ordered list of trade P&Ls.

    Args:
        pnls: ordered (chronological) per-trade realized P&L in INR.
        starting_capital: equity-curve base, used for return_pct and drawdown.
        returns_basis: denominator for per-trade returns feeding Sharpe/Sortino.
            Defaults to starting_capital. Pass the per-trade capital-at-risk
            (e.g. premium paid) for a margin-relative Sharpe.

    Sharpe/Sortino are computed on per-trade returns and annualised by trade
    frequency ((252/n)**0.5), matching core/backtest_engine.py so scores line up.
    A high win-rate with one fat-tail loss scores poorly here — that is the point.
    """
    n = len(pnls)
    if n == 0:
        return _empty()

    basis = returns_basis if (returns_basis and returns_basis > 0) else starting_capital
    pnls = [float(p) for p in pnls]
    total_pnl = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / n

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = 99.0 if gross_profit > 0 else 0.0

    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else (99.0 if avg_win > 0 else 0.0)
    expectancy = total_pnl / n  # average INR gained per trade

    # Equity curve + max drawdown
    equity_curve: List[float] = []
    cum = starting_capital
    peak = starting_capital
    max_dd = 0.0
    for p in pnls:
        cum += p
        equity_curve.append(round(cum, 2))
        if cum > peak:
            peak = cum
        if peak > 0:
            dd = (peak - cum) / peak
            if dd > max_dd:
                max_dd = dd

    # Sharpe / Sortino from per-trade returns
    rets = [p / basis for p in pnls]
    mean_r = sum(rets) / n
    sharpe = 0.0
    sortino = 0.0
    if n >= 2:
        var = sum((r - mean_r) ** 2 for r in rets) / (n - 1)
        std = var ** 0.5
        ann = (TRADING_SESSIONS_PER_YEAR / n) ** 0.5
        sharpe = (mean_r / std) * ann if std > 0 else 0.0
        downside = [r for r in rets if r < 0]
        if downside:
            dvar = sum(r ** 2 for r in downside) / len(downside)
            dstd = dvar ** 0.5
            sortino = (mean_r / dstd) * ann if dstd > 0 else 0.0
        elif mean_r > 0:
            sortino = 99.0

    return {
        "total_trades": n,
        "total_pnl": round(total_pnl, 2),
        "return_pct": round((total_pnl / starting_capital) * 100, 2),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff_ratio": round(payoff_ratio, 2),
        "expectancy": round(expectancy, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "best_trade": round(max(pnls), 2),
        "worst_trade": round(min(pnls), 2),
        "equity_curve": equity_curve,
    }


def grade(metrics: Dict[str, Any]) -> str:
    """Coarse, honest letter grade for at-a-glance ranking. Needs >= 5 trades to
    grade; otherwise INSUFFICIENT (small samples are noise, not signal)."""
    if metrics.get("total_trades", 0) < 5:
        return "INSUFFICIENT"
    sharpe = metrics.get("sharpe", 0.0)
    pf = metrics.get("profit_factor", 0.0)
    exp = metrics.get("expectancy", 0.0)
    if exp <= 0 or pf < 1.0:
        return "F"  # loses money on average
    if sharpe >= 1.5 and pf >= 1.6:
        return "A"
    if sharpe >= 1.0 and pf >= 1.3:
        return "B"
    if sharpe >= 0.5 and pf >= 1.1:
        return "C"
    return "D"
