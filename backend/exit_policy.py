"""ExitPolicy — ATR-based stop-loss, take-profit, and theta guard.

Created at every option-buying entry and attached to the signal so that
PositionGuardian and position_monitor use real ATR prices rather than
the wide percentage defaults.

ATR-based SL rules:
  sl_pct = underlying_atr_pct × ATR_SL_AMPLIFICATION
  sl_pct is then capped to [ATR_SL_MIN_PCT, ATR_SL_MAX_PCT] of premium.
  TP ≥ 1.5R; trail activates at +1R (locks breakeven).

Theta guard:
  Skip option-buying when underlying intraday ATR% < THETA_GUARD_ATR_PCT.
  Rationale: sub-threshold moves cannot cover theta decay, producing near-
  certain losses on bought options.

All thresholds are env-overridable.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("quantg.exit_policy")

# ── Config ────────────────────────────────────────────────────────────────────

# SL = underlying_atr_pct × ATR_SL_AMPLIFICATION (applied to option premium %)
ATR_SL_AMPLIFICATION = float(os.environ.get("EXIT_ATR_SL_AMPLIFICATION", "4.0"))

# Hard floor/ceiling on SL as % of option premium
ATR_SL_MIN_PCT  = float(os.environ.get("EXIT_ATR_SL_MIN_PCT",  "10.0"))
ATR_SL_MAX_PCT  = float(os.environ.get("EXIT_ATR_SL_MAX_PCT",  "32.0"))

# TP = SL × TP_R_MULTIPLE; trail kicks in after +1R
TP_R_MULTIPLE   = float(os.environ.get("EXIT_TP_R_MULTIPLE",   "1.5"))
TRAIL_AFTER_R   = float(os.environ.get("EXIT_TRAIL_AFTER_R",   "1.0"))

# Theta guard: skip if underlying ATR% is below this threshold
THETA_GUARD_ATR_PCT = float(os.environ.get("EXIT_THETA_GUARD_ATR_PCT", "0.08"))


# ── ATR helpers ───────────────────────────────────────────────────────────────

def _true_range(candle: Dict[str, Any], prev_close: float) -> float:
    h = float(candle.get("high") or candle.get("close") or 0)
    l = float(candle.get("low")  or candle.get("close") or 0)
    c = float(candle.get("close") or 0)
    if c <= 0:
        return 0.0
    return max(h - l, abs(h - prev_close), abs(l - prev_close))


def compute_atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
    """Average True Range over `period` bars. Returns 0.0 if not enough data."""
    if len(candles) < period + 1:
        return 0.0
    trs = []
    for j in range(1, len(candles)):
        tr = _true_range(candles[j], float(candles[j - 1].get("close") or 0))
        trs.append(tr)
    recent = trs[-period:]
    return sum(recent) / len(recent)


def compute_atr_pct(candles: List[Dict[str, Any]], period: int = 14) -> float:
    """ATR as % of the last close. Returns 0.0 if data is insufficient."""
    atr = compute_atr(candles, period)
    if not candles:
        return 0.0
    last_close = float(candles[-1].get("close") or 0)
    if last_close <= 0 or atr <= 0:
        return 0.0
    return round(atr / last_close * 100.0, 4)


# ── Theta guard ───────────────────────────────────────────────────────────────

def theta_guard_passes(underlying_atr_pct: float) -> bool:
    """Return True when the underlying is moving enough to justify buying options."""
    return underlying_atr_pct >= THETA_GUARD_ATR_PCT


# ── ExitPolicy factory ────────────────────────────────────────────────────────

def build_exit_policy(
    underlying_atr_pct: float,
    option_premium: float,
) -> Optional[Dict[str, Any]]:
    """Build an ExitPolicy dict for one option-buying entry.

    Returns None when:
      - underlying_atr_pct or option_premium is not positive (can't compute)
      - theta guard fails (caller should skip the entry entirely)

    The returned dict is stored in the signal as ``exit_policy`` and merged
    into tp_sl_tsl_config when the position record is created.
    """
    if underlying_atr_pct <= 0 or option_premium <= 0:
        return None

    # ATR-based SL
    raw_sl_pct = underlying_atr_pct * ATR_SL_AMPLIFICATION
    sl_pct = max(ATR_SL_MIN_PCT, min(ATR_SL_MAX_PCT, raw_sl_pct))
    sl_abs = round(option_premium * sl_pct / 100.0, 2)

    # TP at ≥1.5R
    tp_pct = round(sl_pct * TP_R_MULTIPLE, 2)
    tp_abs = round(sl_abs * TP_R_MULTIPLE, 2)

    # Trail: activates at +1R (= sl_pct gain), then trails sl_pct/2 below peak.
    # trail_trigger_pct and trail_step_pct are the keys read by position_lifecycle.py.
    # trailing_sl_pct is kept for backward-compat but is not consumed by the trail logic.
    trail_pct = round(sl_pct * TRAIL_AFTER_R, 2)

    policy: Dict[str, Any] = {
        "stop_loss_pct":      sl_pct,
        "take_profit_pct":    tp_pct,
        "trailing_sl_pct":    trail_pct,
        "trail_trigger_pct":  trail_pct,
        "trail_step_pct":     round(sl_pct * 0.5, 2),
        "stoploss_price":     round(option_premium - sl_abs, 2),
        "target_price":       round(option_premium + tp_abs, 2),
        "protection_status":  "PROTECTED_ATR_POLICY",
        "atr_pct_used":       underlying_atr_pct,
        "amplification":      ATR_SL_AMPLIFICATION,
    }
    return policy


# ── Signal attachment helpers ─────────────────────────────────────────────────

def attach_exit_policy_to_signal(
    signal: Dict[str, Any],
    candles: List[Dict[str, Any]],
    option_premium: float,
    atr_period: int = 14,
) -> Dict[str, Any]:
    """Compute ATR from candles and attach exit_policy to the signal.

    Returns the (possibly augmented) signal unchanged if data is insufficient
    or theta guard blocks the trade (caller checks ``theta_guard_blocked``).
    """
    signal = dict(signal)
    atr_pct = compute_atr_pct(candles, period=atr_period)

    # Always embed the ATR reading for diagnostics
    signal["underlying_atr_pct"] = atr_pct

    # Theta guard
    if not theta_guard_passes(atr_pct):
        logger.info(
            "Theta guard BLOCKED: atr_pct=%.4f < threshold=%.4f for signal action=%s",
            atr_pct, THETA_GUARD_ATR_PCT, signal.get("action"),
        )
        signal["theta_guard_blocked"] = True
        return signal

    # Build policy
    policy = build_exit_policy(atr_pct, option_premium)
    if policy:
        signal["exit_policy"] = policy
        signal["sl_pct_atr"]  = policy["stop_loss_pct"]
        signal["tp_pct_atr"]  = policy["take_profit_pct"]
    return signal
