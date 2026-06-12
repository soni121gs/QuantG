"""Position lifecycle — pure risk calculation and exit-reason functions.

Extracted from server.py so they can be shared by the monitor loop,
the exit handler, and any future modules without circular imports.

No DB access. No server.py imports. All functions are pure or near-pure.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("quantg.position_lifecycle")

# Trade-frequency categories (audit #9). A strategy's category is independent of
# its risk_style: it governs how often the strategy may trade, not its SL/TP
# profile. Scalpers must trade frequently, so their cooldown/trade caps are
# bounded here and enforced in normalize_strategy_risk.
STRATEGY_CATEGORIES = ("scalper", "intraday", "swing")
SCALPER_MAX_COOLDOWN_MIN = 3   # a scalper may not be throttled beyond 3 min
SCALPER_MIN_TRADES_DAY = 5     # a scalper must be allowed at least 5 trades/day

# ── Default risk config ────────────────────────────────────────────────────────

DEFAULT_STRATEGY_RISK: Dict[str, Any] = {
    "stop_loss_pct": 8.0,
    "take_profit_pct": 12.0,
    "trail_trigger_pct": 5.5,
    "trail_step_pct": 3.0,
    "cooldown_minutes": 15,
    "max_trades_day": 3,
    "daily_loss_limit": 750.0,
    "time_exit_minutes": 22,
    "indicator_exit_enabled": True,
    "exit_mode": "tp_sl_tsl_or_signal",
    "pause_on_issue": True,
    "risk_style": "balanced",
    "adaptive_exits_enabled": True,
    "target_r_multiple": 1.45,
}

# ── Utility ────────────────────────────────────────────────────────────────────

def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _risk_pct(risk: Dict[str, Any], *keys: str, default: Optional[float]) -> Optional[float]:
    for key in keys:
        value = risk.get(key)
        if value not in (None, ""):
            try:
                pct = float(value)
                return pct if pct > 1 else pct * 100.0
            except (TypeError, ValueError):
                continue
    return float(default) if default is not None else None


def parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


# ── Risk normalisation ─────────────────────────────────────────────────────────

def adaptive_risk_percentages(entry: float, risk: Dict[str, Any]) -> Dict[str, Optional[float]]:
    stop = float(risk["stop_loss_pct"]) if risk.get("stop_loss_pct") not in (None, "") else None
    target = float(risk["take_profit_pct"]) if risk.get("take_profit_pct") not in (None, "") else None
    trigger = float(risk["trail_trigger_pct"])
    step = float(risk["trail_step_pct"])
    if stop is None or target is None or not risk.get("adaptive_exits_enabled", True) or entry <= 0:
        return {"stop": stop, "target": target, "trigger": trigger, "step": step}
    style = str(risk.get("risk_style") or "balanced")
    bounds = {
        "micro_scalp":      (3.5,  7.5,  1.15, 1.35),
        "momentum":         (4.5,  9.5,  1.25, 1.55),
        "breakout":         (5.5, 11.5,  1.35, 1.70),
        "volatile_breakout":(6.5, 13.5,  1.40, 1.85),
        "pullback":         (4.5,  9.0,  1.25, 1.55),
        "balanced":         (4.5, 10.0,  1.25, 1.55),
    }
    min_stop, max_stop, min_r, max_r = bounds.get(style, bounds["balanced"])
    premium_factor = 1.18 if entry < 75 else 0.88 if entry > 250 else 1.0
    stop = _clamp(stop * premium_factor, min_stop, max_stop)
    r_multiple = _clamp(float(risk.get("target_r_multiple") or min_r), min_r, max_r)
    target = _clamp(max(target, stop * r_multiple), stop * min_r, stop * max_r)
    trigger = _clamp(min(trigger, stop * 0.75), 2.5, max(3.0, stop * 0.95))
    step = _clamp(min(step, stop * 0.45), 1.5, max(2.0, stop * 0.65))
    return {"stop": stop, "target": target, "trigger": trigger, "step": step}


def normalize_strategy_risk(risk: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    raw = dict(risk or {})
    stop_pct = _risk_pct(raw, "stop_loss_pct", "stoploss_pct", "stop_pct", default=None)
    target_pct = _risk_pct(raw, "take_profit_pct", "target_pct", "tp_pct", default=None)
    trail_trigger_pct = _risk_pct(raw, "trail_trigger_pct", default=DEFAULT_STRATEGY_RISK["trail_trigger_pct"])
    trail_step_pct = _risk_pct(raw, "trail_step_pct", default=DEFAULT_STRATEGY_RISK["trail_step_pct"])
    risk_style = str(raw.get("risk_style") or DEFAULT_STRATEGY_RISK["risk_style"])
    target_r_multiple = float(raw.get("target_r_multiple") or DEFAULT_STRATEGY_RISK["target_r_multiple"])
    raw.update({
        "trail_trigger_pct": trail_trigger_pct,
        "trail_step_pct": trail_step_pct,
        "trailing_sl_enabled": bool(raw.get("trailing_sl_enabled", True)),
        "cooldown_minutes": int(raw.get("cooldown_minutes") or DEFAULT_STRATEGY_RISK["cooldown_minutes"]),
        "max_trades_day": int(raw.get("max_trades_day") or DEFAULT_STRATEGY_RISK["max_trades_day"]),
        "daily_loss_limit": float(raw.get("daily_loss_limit") or DEFAULT_STRATEGY_RISK["daily_loss_limit"]),
        "time_exit_minutes": int(raw.get("time_exit_minutes") or DEFAULT_STRATEGY_RISK["time_exit_minutes"]),
        "indicator_exit_enabled": bool(raw.get("indicator_exit_enabled", DEFAULT_STRATEGY_RISK["indicator_exit_enabled"])),
        "exit_mode": raw.get("exit_mode") or DEFAULT_STRATEGY_RISK["exit_mode"],
        "risk_style": risk_style,
        "adaptive_exits_enabled": bool(raw.get("adaptive_exits_enabled", DEFAULT_STRATEGY_RISK["adaptive_exits_enabled"])),
        "target_r_multiple": target_r_multiple,
    })
    # Trade-frequency category guardrail (audit #9). Independent of risk_style
    # (which governs the SL/TP exit profile). A "scalper" must be allowed to
    # trade frequently — clamp swing-like cooldown/trade caps so a scalper can
    # never be saved with throttling limits. Clamp (never raise): this function
    # runs on every position read, so it must stay total over legacy data.
    category = str(raw.get("strategy_category") or "").strip().lower()
    if category in ("scalper", "intraday", "swing"):
        raw["strategy_category"] = category
        if category == "scalper":
            if raw["cooldown_minutes"] > SCALPER_MAX_COOLDOWN_MIN:
                raw["cooldown_minutes"] = SCALPER_MAX_COOLDOWN_MIN
            if raw["max_trades_day"] < SCALPER_MIN_TRADES_DAY:
                raw["max_trades_day"] = SCALPER_MIN_TRADES_DAY
    if stop_pct is not None:
        raw["stop_loss_pct"] = stop_pct
        raw["stoploss_pct"] = stop_pct
    if target_pct is not None:
        raw["take_profit_pct"] = target_pct
        raw["target_pct"] = target_pct
    return raw


def position_risk_prices(position: Dict[str, Any], ltp: Optional[float] = None) -> Dict[str, Optional[float]]:
    entry = float(position.get("average_buy_price") or 0)
    if entry <= 0:
        return {"stop_loss": None, "take_profit": None, "trailing_sl": None}
    risk = normalize_strategy_risk(position.get("tp_sl_tsl_config") or {})
    side = str(position.get("position_side") or "LONG").upper()
    stop_price = risk.get("stoploss_price") or risk.get("stop_loss")
    target_price = risk.get("target_price") or risk.get("take_profit")
    dynamic = adaptive_risk_percentages(entry, risk)
    if stop_price in (None, ""):
        stop_pct = risk.get("stop_loss_pct")
        if stop_pct is not None:
            stop_pct = float(stop_pct)
            stop_price = entry * (1 - stop_pct / 100) if side != "SHORT" else entry * (1 + stop_pct / 100)
    if target_price in (None, ""):
        target_pct = risk.get("take_profit_pct")
        if target_pct is not None:
            target_pct = float(target_pct)
            target_price = entry * (1 + target_pct / 100) if side != "SHORT" else entry * (1 - target_pct / 100)
    trailing_sl = risk.get("trailing_sl")
    if risk.get("trailing_sl_enabled") and ltp and ltp > 0:
        trigger_pct = dynamic["trigger"]
        step_pct = dynamic["step"]
        if side == "SHORT" and ltp <= entry * (1 - trigger_pct / 100):
            candidate = ltp * (1 + step_pct / 100)
            trailing_sl = min(float(trailing_sl or candidate), candidate)
        elif side != "SHORT" and ltp >= entry * (1 + trigger_pct / 100):
            candidate = ltp * (1 - step_pct / 100)
            trailing_sl = max(float(trailing_sl or 0), candidate)
    # Enforce breakeven floor: once trailing stop activates it must never fall below entry
    if trailing_sl is not None and trailing_sl > 0:
        if side != "SHORT":
            trailing_sl = max(trailing_sl, entry)
        else:
            trailing_sl = min(trailing_sl, entry)
    effective_stop = trailing_sl or stop_price
    return {
        "stop_loss":  round(float(effective_stop), 2) if effective_stop not in (None, "") else None,
        "take_profit": round(float(target_price), 2) if target_price not in (None, "") else None,
        "trailing_sl": round(float(trailing_sl), 2) if trailing_sl not in (None, "") else None,
    }


def exit_reason(position: Dict[str, Any], ltp: float) -> Optional[str]:
    """Return the exit reason string if the position should be closed, else None."""
    entry = float(position.get("average_buy_price") or 0)
    if entry <= 0 or ltp <= 0:
        return None
    risk = normalize_strategy_risk(position.get("tp_sl_tsl_config") or {})
    side = str(position.get("position_side") or "LONG").upper()
    prices = position_risk_prices({**position, "tp_sl_tsl_config": risk}, ltp=ltp)
    stop_price = prices.get("stop_loss")
    target_price = prices.get("take_profit")
    if stop_price is None and target_price is None:
        return None
    if stop_price is not None:
        stop_price = float(stop_price)
        if side == "SHORT":
            if ltp >= stop_price:
                return "trailing-sl" if prices.get("trailing_sl") else "stop-loss"
        else:
            if ltp <= stop_price:
                return "trailing-sl" if prices.get("trailing_sl") else "stop-loss"
    if target_price is not None:
        target_price = float(target_price)
        if side == "SHORT":
            if ltp <= target_price:
                return "take-profit"
        else:
            if ltp >= target_price:
                return "take-profit"
    time_exit_minutes = int(risk.get("time_exit_minutes") or 0)
    if time_exit_minutes > 0:
        entry_dt = parse_iso_dt(position.get("entry_time"))
        if entry_dt and (datetime.now(timezone.utc) - entry_dt).total_seconds() >= time_exit_minutes * 60:
            return f"time-exit-{time_exit_minutes}m"
    return None
