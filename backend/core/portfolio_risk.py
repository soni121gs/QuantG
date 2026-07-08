"""RES-6 — portfolio risk layer (heat cap + correlation guard + daily-loss gate).

The best edge blows up without this. Today's −₹4,571 was exactly a portfolio
failure: two of three positions were the SAME NIFTY bull-put bet, so one selloff
hit them together. This module caps three things at the BOOK level (not just
per-trade):

  1. Heat      — total defined risk across all open positions ≤ a budget.
  2. Correlation — at most N positions sharing one directional bias per underlying.
  3. Daily loss — realized P&L today ≥ −limit (a hard stop for the day).

PURE functions so the live entry path and the backtester enforce the same caps.
A correlation guard (`MAX_DIRECTIONAL_EXPOSURE_PER_UNDERLYING`) already exists in
strategy_runner; this consolidates the book-level view the scalper (RES-7) uses.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

# Max total defined risk (₹) the book may hold open at once.
PORTFOLIO_HEAT_BUDGET = float(os.environ.get("PORTFOLIO_HEAT_BUDGET", "60000"))
# Max open positions sharing one bias (BULLISH/BEARISH) on a single underlying.
MAX_CORRELATED_PER_UNDERLYING = int(os.environ.get("MAX_CORRELATED_PER_UNDERLYING", "2"))
# Daily realized-loss stop (₹, positive number).
DAILY_LOSS_LIMIT = float(os.environ.get("PORTFOLIO_DAILY_LOSS_LIMIT", "15000"))


def position_risk(pos: Dict[str, Any]) -> float:
    """Defined risk (₹) of one open position. Spreads carry `max_loss_total`;
    a long option risks its premium; fall back to planned_risk / notional."""
    for k in ("max_loss_total", "planned_risk"):
        v = pos.get(k)
        if v is not None:
            try:
                return abs(float(v))
            except (TypeError, ValueError):
                pass
    # long option / equity fallback: entry price × qty
    px = float(pos.get("average_price") or pos.get("entry_price") or 0.0)
    qty = float(pos.get("open_quantity") or pos.get("quantity") or 0.0)
    return abs(px * qty)


def book_heat(open_positions: Sequence[Dict[str, Any]]) -> float:
    """Sum of defined risk across all open positions."""
    return round(sum(position_risk(p) for p in (open_positions or [])), 2)


def evaluate_heat(
    open_positions: Sequence[Dict[str, Any]],
    new_risk: float,
    heat_budget: float = PORTFOLIO_HEAT_BUDGET,
) -> Dict[str, Any]:
    """Would adding a trade of `new_risk` breach the book heat budget?"""
    current = book_heat(open_positions)
    projected = round(current + abs(float(new_risk)), 2)
    allow = projected <= float(heat_budget)
    return {"allow": allow, "current_heat": current, "projected_heat": projected,
            "budget": float(heat_budget),
            "reason": (None if allow else
                       f"HEAT_CAP: projected ₹{projected:,.0f} > budget ₹{heat_budget:,.0f}")}


def evaluate_correlation(
    open_positions: Sequence[Dict[str, Any]],
    *,
    underlying: str,
    new_bias: str,
    max_same: int = MAX_CORRELATED_PER_UNDERLYING,
) -> Dict[str, Any]:
    """Would adding a `new_bias` position on `underlying` exceed the same-bias
    cap? Positions carry a `bias` field (BULLISH/BEARISH); we count matches on
    the same underlying."""
    bias = str(new_bias or "").upper()
    count = sum(
        1 for p in (open_positions or [])
        if str(p.get("underlying") or p.get("symbol") or "").upper() == str(underlying).upper()
        and str(p.get("bias") or "").upper() == bias
    )
    allow = count < int(max_same)
    return {"allow": allow, "same_bias_open": count, "max_same": int(max_same),
            "reason": (None if allow else
                       f"CORRELATION_CAP: {count} {bias} already open on {underlying} (max {max_same})")}


def evaluate_daily_loss(realized_today: float, limit: float = DAILY_LOSS_LIMIT) -> Dict[str, Any]:
    """Is the day's realized loss within the hard stop? `realized_today` is signed
    (negative = loss)."""
    r = float(realized_today)
    allow = r > -abs(float(limit))
    return {"allow": allow, "realized_today": round(r, 2), "limit": abs(float(limit)),
            "reason": (None if allow else
                       f"DAILY_LOSS_STOP: realized ₹{r:,.0f} ≤ −₹{abs(limit):,.0f}")}


def evaluate_portfolio_gate(
    open_positions: Sequence[Dict[str, Any]],
    *,
    new_risk: float,
    underlying: str,
    new_bias: str,
    realized_today: float = 0.0,
    heat_budget: float = PORTFOLIO_HEAT_BUDGET,
    max_same: int = MAX_CORRELATED_PER_UNDERLYING,
    daily_loss_limit: float = DAILY_LOSS_LIMIT,
) -> Dict[str, Any]:
    """Combined book-level gate. allow only if heat, correlation AND daily-loss all
    pass. Returns the first failing reason (and all sub-checks for diagnostics)."""
    heat = evaluate_heat(open_positions, new_risk, heat_budget)
    corr = evaluate_correlation(open_positions, underlying=underlying,
                                new_bias=new_bias, max_same=max_same)
    loss = evaluate_daily_loss(realized_today, daily_loss_limit)
    blockers: List[str] = [c["reason"] for c in (loss, heat, corr) if not c["allow"]]
    return {"allow": not blockers, "reason": (blockers[0] if blockers else None),
            "checks": {"heat": heat, "correlation": corr, "daily_loss": loss}}
