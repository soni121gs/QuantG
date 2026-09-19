"""Read-only portfolio stress scenarios based on persisted Greeks."""
from __future__ import annotations

from typing import Any, Dict, List


def _num(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _greek(pos: Dict[str, Any], name: str) -> float | None:
    for source in (pos.get("greeks"), pos.get("greeks_at_signal"), pos):
        if isinstance(source, dict):
            value = _num(source.get(name))
            if value is not None:
                return value
    return None


def scenario_position(pos: Dict[str, Any], *, move_pct: float, iv_points: float, days: float) -> Dict[str, Any]:
    spot = _num(pos.get("scenario_spot") if pos.get("scenario_spot") is not None else pos.get("underlying_price"))
    delta, gamma, theta, vega = (_greek(pos, x) for x in ("delta", "gamma", "theta", "vega"))
    missing = [name for name, value in (("spot", spot), ("delta", delta), ("gamma", gamma), ("theta", theta), ("vega", vega)) if value is None]
    if missing:
        return {"position_id": pos.get("id"), "status": "NOT_COMPUTABLE", "missing": missing}
    quantity = _num(pos.get("open_quantity") if pos.get("open_quantity") is not None else pos.get("quantity")) or 1.0
    move = spot * float(move_pct)
    per_unit = delta * move + 0.5 * gamma * move * move + vega * float(iv_points) + theta * float(days)
    return {"position_id": pos.get("id"), "status": "ESTIMATE", "estimated_pnl": round(per_unit * quantity, 2), "quantity": quantity, "missing": []}


def build_portfolio_scenario(positions: List[Dict[str, Any]], *, move_pct: float = 0.0, iv_points: float = 0.0, days: float = 0.0) -> Dict[str, Any]:
    rows = [scenario_position(p, move_pct=move_pct, iv_points=iv_points, days=days) for p in positions]
    estimates = [r["estimated_pnl"] for r in rows if r["status"] == "ESTIMATE"]
    return {
        "read_only": True,
        "status": "ESTIMATE" if len(estimates) == len(rows) else "PARTIAL",
        "scenario": {"underlying_move_pct": float(move_pct), "iv_change_points": float(iv_points), "holding_days": float(days)},
        "estimated_pnl": round(sum(estimates), 2) if estimates else None,
        "positions": rows,
        "coverage": {"computed": len(estimates), "total": len(rows), "missing_inputs": sum(1 for r in rows if r["status"] == "NOT_COMPUTABLE")},
        "calculation_basis": "Persisted Greeks approximation: delta*dS + 0.5*gamma*dS^2 + vega*dIV + theta*days.",
        "warning": "Stress estimate only. It is not a forecast, valuation, order recommendation, or execution gate.",
    }


async def load_portfolio_scenario(db: Any, user_id: str, *, move_pct: float = 0.0, iv_points: float = 0.0, days: float = 0.0) -> Dict[str, Any]:
    positions = await db.strategy_positions.find({"user_id": user_id, "status": {"$in": ["OPEN", "FILLED", "EXITING", "PENDING_OPEN", "PENDING_BROKER", "RESERVED"]}}, {"_id": 0, "user_id": 0}).to_list(5000)
    return build_portfolio_scenario(positions, move_pct=move_pct, iv_points=iv_points, days=days)
