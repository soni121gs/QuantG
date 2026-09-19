"""Deterministic, evidence-linked narration for whole-portfolio data."""
from __future__ import annotations

from typing import Any, Dict


def _money(value: Any) -> str:
    try:
        return f"₹{float(value):,.2f}"
    except (TypeError, ValueError):
        return "unavailable"


def explain_portfolio(snapshot: Dict[str, Any], scenario: Dict[str, Any] | None = None) -> str:
    if not snapshot:
        return "Portfolio snapshot is unavailable."
    lines = [
        f"Portfolio: {snapshot.get('open_positions', 0)} open positions, defined risk {_money(snapshot.get('defined_risk'))}, total P&L {_money(snapshot.get('total_pnl'))}.",
        f"P&L split: realized {_money(snapshot.get('realized_pnl'))}; unrealized {_money(snapshot.get('unrealized_pnl'))}.",
    ]
    rows = snapshot.get("by_underlying") or []
    if rows:
        lead = rows[0]
        lines.append(f"Largest defined-risk underlying: {lead.get('name', 'UNKNOWN')} at {_money(lead.get('risk'))} across {lead.get('positions', 0)} position(s).")
    missing = ((snapshot.get("data_quality") or {}).get("missing_greeks") or [])
    if missing:
        lines.append(f"Data limitation: Greeks are incomplete for {', '.join(missing)}; no missing value was inferred.")
    if scenario is not None:
        if scenario.get("estimated_pnl") is None:
            lines.append("Scenario result: NOT COMPUTABLE because required persisted inputs are missing.")
        else:
            s = scenario.get("scenario") or {}
            lines.append(f"Scenario result: {_money(scenario.get('estimated_pnl'))} estimated P&L impact for {float(s.get('underlying_move_pct', 0)) * 100:g}% underlying move, {s.get('iv_change_points', 0):g} IV points and {s.get('holding_days', 0):g} day(s).")
    lines.append("This is read-only evidence and an approximation where stated; it is not an order recommendation or execution gate.")
    return "\n".join(lines)
