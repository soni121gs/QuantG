"""Pre-trade risk controls for QuantG order sizing and market-data gating."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import floor
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class SizeInputs:
    equity: float
    free_margin: float
    requested_qty: int
    lot_size: int
    entry_price: float
    stop_loss_price: Optional[float]
    max_position_value: float
    daily_loss_limit: float
    risk_style: str = "balanced"
    margin_buffer: float = 0.85


@dataclass(frozen=True)
class SizeResult:
    allowed: bool
    quantity: int
    reason: str
    risk_budget: float
    unit_loss_at_stop: float
    order_value: float
    caps: Dict[str, int]


RISK_FRACTION_BY_STYLE = {
    "micro_scalp": 0.020,
    "momentum": 0.025,
    "breakout": 0.020,
    "volatile_breakout": 0.015,
    "pullback": 0.020,
    "balanced": 0.020,
}


MAX_STALE_SEC_BY_STYLE = {
    "micro_scalp": 20,
    "momentum": 45,
    "breakout": 60,
    "volatile_breakout": 75,
    "pullback": 60,
    "balanced": 60,
}


MAX_SPREAD_BPS_BY_STYLE = {
    "micro_scalp": 45,
    "momentum": 65,
    "breakout": 80,
    "volatile_breakout": 100,
    "pullback": 70,
    "balanced": 75,
}


def _positive(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _round_to_lot(quantity: int, lot_size: int) -> int:
    lot = max(1, int(lot_size or 1))
    return max(0, (max(0, int(quantity or 0)) // lot) * lot)


def compute_position_size(inputs: SizeInputs) -> SizeResult:
    requested = max(0, int(inputs.requested_qty or 0))
    lot_size = max(1, int(inputs.lot_size or 1))
    entry = _positive(inputs.entry_price)
    equity = _positive(inputs.equity)
    free_margin = _positive(inputs.free_margin, equity)
    max_position_value = _positive(inputs.max_position_value, equity)
    daily_loss_limit = _positive(inputs.daily_loss_limit)
    style = str(inputs.risk_style or "balanced")

    if requested <= 0:
        return SizeResult(False, 0, "requested quantity is zero", 0.0, 0.0, 0.0, {})
    if entry <= 0:
        return SizeResult(False, 0, "entry price unavailable", 0.0, 0.0, 0.0, {})

    stop = _positive(inputs.stop_loss_price)
    unit_loss = abs(entry - stop) if stop > 0 else max(entry * 0.06, 1.0)
    risk_fraction = RISK_FRACTION_BY_STYLE.get(style, RISK_FRACTION_BY_STYLE["balanced"])
    base_risk_budget = equity * risk_fraction
    risk_budget = min(base_risk_budget, daily_loss_limit * 0.35) if daily_loss_limit > 0 else base_risk_budget

    qty_risk = floor(risk_budget / unit_loss) if unit_loss > 0 else 0
    qty_margin = floor((free_margin * float(inputs.margin_buffer or 0.85)) / entry) if free_margin > 0 else requested
    qty_value = floor(max_position_value / entry) if max_position_value > 0 else requested
    qty_requested = requested

    caps = {
        "requested": _round_to_lot(qty_requested, lot_size),
        "risk": _round_to_lot(qty_risk, lot_size),
        "margin": _round_to_lot(qty_margin, lot_size),
        "position_value": _round_to_lot(qty_value, lot_size),
    }
    quantity = min(caps.values()) if caps else 0
    order_value = round(quantity * entry, 2)

    if quantity < lot_size:
        return SizeResult(False, 0, "risk budget, margin, or max position cap allows less than one lot", risk_budget, unit_loss, order_value, caps)
    if quantity < requested:
        return SizeResult(True, quantity, f"quantity reduced from {requested} to {quantity} by pre-trade risk caps", risk_budget, unit_loss, order_value, caps)
    return SizeResult(True, quantity, "accepted", risk_budget, unit_loss, order_value, caps)


def parse_market_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        if number > 10_000_000_000:
            number = number / 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def evaluate_market_data_quality(
    *,
    ltp: float,
    tick_time: Any = None,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    reference_price: Optional[float] = None,
    risk_style: str = "balanced",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    price = _positive(ltp)
    style = str(risk_style or "balanced")
    if price <= 0:
        return {"ok": False, "reason": "ltp unavailable", "checks": {"ltp": price}}

    now = now or datetime.now(timezone.utc)
    max_stale = MAX_STALE_SEC_BY_STYLE.get(style, MAX_STALE_SEC_BY_STYLE["balanced"])
    parsed_tick_time = parse_market_timestamp(tick_time)
    age_sec = None
    if parsed_tick_time:
        age_sec = max(0.0, (now - parsed_tick_time).total_seconds())
        if age_sec > max_stale:
            return {"ok": False, "reason": f"market data stale: last tick age {int(age_sec)}s > {max_stale}s", "checks": {"ltp": price, "age_sec": age_sec, "max_stale_sec": max_stale}}

    bid_price = _positive(bid)
    ask_price = _positive(ask)
    if bid_price > 0 and ask_price > 0 and ask_price >= bid_price:
        spread_bps = ((ask_price - bid_price) / price) * 10000
        max_spread = MAX_SPREAD_BPS_BY_STYLE.get(style, MAX_SPREAD_BPS_BY_STYLE["balanced"])
        if spread_bps > max_spread:
            return {"ok": False, "reason": f"spread too wide: {spread_bps:.0f} bps > {max_spread} bps", "checks": {"spread_bps": round(spread_bps, 2), "max_spread_bps": max_spread}}

    ref = _positive(reference_price)
    if ref > 0:
        move_bps = abs(price - ref) / ref * 10000
        if move_bps > 500:
            return {"ok": False, "reason": f"price jump too large: {move_bps:.0f} bps from reference", "checks": {"move_bps": round(move_bps, 2)}}

    return {"ok": True, "reason": "accepted", "checks": {"ltp": price, "age_sec": age_sec, "max_stale_sec": max_stale}}
