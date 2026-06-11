"""MarketRegime service — per-index regime computation from intraday OHLCV.

Computes CRASH / MELTUP / TREND_UP / TREND_DOWN / RANGE every runner tick.
The CRASH/MELTUP gate uses the live intraday return so a flat-open session
that crashes at 11:00 flips immediately (no 15-min lag). The full bias
(VWAP slope, EMA alignment) refreshes every REGIME_REFRESH_MINUTES.

Regime → strategy gate rules (enforced in strategy_runner.py):
  CRASH   → long_entries_allowed=False, +10 confidence to aligned shorts
  MELTUP  → short_entries_allowed=False, +10 confidence to aligned longs
  TREND   → hold_multiplier=2.0, freq_multiplier=1.0
  RANGE   → freq_multiplier=0.5, hold_multiplier=0.8

DB persistence: db.market_regime_state (for status endpoint + cross-pod read).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("quantg.market_regime")

# ── Config ────────────────────────────────────────────────────────────────────
CRASH_THRESHOLD_PCT   = float(os.environ.get("REGIME_CRASH_PCT",   "-1.5"))
MELTUP_THRESHOLD_PCT  = float(os.environ.get("REGIME_MELTUP_PCT",  "1.5"))
TREND_THRESHOLD_PCT   = float(os.environ.get("REGIME_TREND_PCT",   "0.5"))
REGIME_REFRESH_MINUTES = int(os.environ.get("REGIME_REFRESH_MIN",  "15"))

VALID_INDICES = {"NIFTY", "BANKNIFTY", "SENSEX"}

# ── In-memory state ───────────────────────────────────────────────────────────
# {index: {"regime": RegimeState, "last_full_compute": monotonic, "last_return_check": float}}
_state: Dict[str, Dict[str, Any]] = {}
_lock = asyncio.Lock()


def _make_regime(
    index: str,
    regime: str,
    bias: str,
    intraday_return_pct: float,
    long_entries_allowed: bool,
    short_entries_allowed: bool,
    hold_multiplier: float,
    freq_multiplier: float,
    vwap: float = 0.0,
    vwap_slope: float = 0.0,
    computed_at: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "index": index,
        "regime": regime,
        "bias": bias,
        "intraday_return_pct": round(intraday_return_pct, 3),
        "long_entries_allowed": long_entries_allowed,
        "short_entries_allowed": short_entries_allowed,
        "hold_multiplier": hold_multiplier,
        "freq_multiplier": freq_multiplier,
        "vwap": round(vwap, 2),
        "vwap_slope": round(vwap_slope, 4),
        "computed_at": computed_at or datetime.now(timezone.utc).isoformat(),
    }


def compute_regime_from_data(index: str, candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure function — no DB, no I/O. Returns regime dict.

    candles: list of {date, open, high, low, close, volume} dicts,
             5-minute bars newest-last, may span multiple days.
    """
    if not candles:
        return _make_regime(index, "RANGE", "NEUTRAL", 0.0, True, True, 1.0, 1.0)

    closes = [float(c.get("close") or 0) for c in candles]
    highs  = [float(c.get("high", c.get("close")) or 0) for c in candles]
    lows   = [float(c.get("low",  c.get("close")) or 0) for c in candles]
    vols   = [max(1.0, float(c.get("volume") or 1)) for c in candles]
    dates  = [str(c.get("date") or "") for c in candles]

    # ── Identify today's candles ──────────────────────────────────────────────
    if not dates[-1]:
        return _make_regime(index, "RANGE", "NEUTRAL", 0.0, True, True, 1.0, 1.0)

    today_str = dates[-1][:10]
    today_idx = [i for i, d in enumerate(dates) if d[:10] == today_str]
    if not today_idx:
        return _make_regime(index, "RANGE", "NEUTRAL", 0.0, True, True, 1.0, 1.0)

    today_start = today_idx[0]
    prev_close = closes[today_start - 1] if today_start > 0 else 0.0
    today_open = float(candles[today_start].get("open") or closes[today_start])
    current_price = closes[-1]

    # ── Intraday return from prev close ──────────────────────────────────────
    intraday_ret_pct = 0.0
    if prev_close > 0:
        intraday_ret_pct = (current_price - prev_close) / prev_close * 100.0

    # ── Day-anchored VWAP (today only) ────────────────────────────────────────
    w_sum = 0.0
    v_sum = 0.0
    vwap_list = []
    for i in today_idx:
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        w_sum += tp * vols[i]
        v_sum += vols[i]
        vwap_list.append(w_sum / max(1.0, v_sum))
    vwap = vwap_list[-1] if vwap_list else current_price

    # ── VWAP slope (last 3 VWAP values, ~15 min) ──────────────────────────────
    vwap_slope = 0.0
    if len(vwap_list) >= 4:
        vwap_slope = (vwap_list[-1] - vwap_list[-4]) / max(0.01, vwap_list[-4]) * 100.0

    # ── EMA 20 on today's closes ──────────────────────────────────────────────
    today_closes = closes[today_start:]
    ema20 = today_closes[0]
    for c in today_closes[1:]:
        ema20 = c * (2 / 21) + ema20 * (19 / 21)

    # ── CRASH / MELTUP (continuous, highest priority) ─────────────────────────
    if intraday_ret_pct <= CRASH_THRESHOLD_PCT:
        return _make_regime(
            index, "CRASH", "BEAR", intraday_ret_pct,
            long_entries_allowed=False,
            short_entries_allowed=True,
            hold_multiplier=1.5,
            freq_multiplier=1.0,
            vwap=vwap, vwap_slope=vwap_slope,
        )

    if intraday_ret_pct >= MELTUP_THRESHOLD_PCT:
        return _make_regime(
            index, "MELTUP", "BULL", intraday_ret_pct,
            long_entries_allowed=True,
            short_entries_allowed=False,
            hold_multiplier=1.5,
            freq_multiplier=1.0,
            vwap=vwap, vwap_slope=vwap_slope,
        )

    # ── TREND_UP ──────────────────────────────────────────────────────────────
    if (intraday_ret_pct >= TREND_THRESHOLD_PCT
            and current_price > vwap
            and vwap_slope > 0
            and current_price > ema20):
        return _make_regime(
            index, "TREND_UP", "BULL", intraday_ret_pct,
            long_entries_allowed=True,
            short_entries_allowed=True,
            hold_multiplier=2.0,
            freq_multiplier=1.0,
            vwap=vwap, vwap_slope=vwap_slope,
        )

    # ── TREND_DOWN ────────────────────────────────────────────────────────────
    if (intraday_ret_pct <= -TREND_THRESHOLD_PCT
            and current_price < vwap
            and vwap_slope < 0
            and current_price < ema20):
        return _make_regime(
            index, "TREND_DOWN", "BEAR", intraday_ret_pct,
            long_entries_allowed=True,
            short_entries_allowed=True,
            hold_multiplier=2.0,
            freq_multiplier=1.0,
            vwap=vwap, vwap_slope=vwap_slope,
        )

    # ── RANGE (default) ───────────────────────────────────────────────────────
    bias = "BULL" if current_price > vwap else ("BEAR" if current_price < vwap else "NEUTRAL")
    return _make_regime(
        index, "RANGE", bias, intraday_ret_pct,
        long_entries_allowed=True,
        short_entries_allowed=True,
        hold_multiplier=0.8,
        freq_multiplier=0.5,
        vwap=vwap, vwap_slope=vwap_slope,
    )


async def update_regime(
    db,
    index: str,
    candles: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute and cache the regime. Persists to DB for status endpoint.

    Returns the current RegimeState dict.
    """
    index = index.upper()
    if index not in VALID_INDICES:
        return _make_regime(index, "RANGE", "NEUTRAL", 0.0, True, True, 1.0, 1.0)

    regime = compute_regime_from_data(index, candles)

    async with _lock:
        cached = _state.get(index, {})
        old_regime = (cached.get("regime") or {}).get("regime")
        _state[index] = {
            "regime": regime,
            "last_update": time.monotonic(),
        }

    if old_regime and old_regime != regime["regime"]:
        logger.warning(
            "REGIME FLIP %s: %s → %s (intraday_return=%.2f%%)",
            index, old_regime, regime["regime"], regime["intraday_return_pct"],
        )

    # Persist to DB (fire-and-forget; don't block strategy runner)
    if db is not None:
        try:
            await db.market_regime_state.update_one(
                {"index": index},
                {"$set": {**regime, "updated_at": datetime.now(timezone.utc).isoformat()}},
                upsert=True,
            )
        except Exception as exc:
            logger.debug("regime DB persist failed for %s: %s", index, exc)

    return regime


def get_cached_regime(index: str) -> Optional[Dict[str, Any]]:
    """Return the last known regime for this index (may be None if never computed)."""
    entry = _state.get(index.upper())
    return entry["regime"] if entry else None


def get_all_regimes() -> Dict[str, Any]:
    return {k: v["regime"] for k, v in _state.items()}


# ── Regime → capital weight multiplier (TASK-015) ─────────────────────────────
# Converts the binary ALLOW/BLOCK gate into a graduated capital weight.
# The hard BLOCK for CRASH/MELTUP entry direction is preserved in strategy_runner.py.
# This table applies an additional capital weight BEFORE the block check so that
# aligned strategies get more capital and misaligned ones get less without a hard stop.

_REGIME_MULTIPLIER_TABLE: Dict[str, Dict[str, float]] = {
    # regime            trend   range  breakout  neutral
    "TREND_UP":   {"trend": 1.3, "range": 0.5, "breakout": 1.0, "neutral": 1.0},
    "TREND_DOWN": {"trend": 1.3, "range": 0.5, "breakout": 1.0, "neutral": 1.0},
    "MELTUP":     {"trend": 1.3, "range": 0.5, "breakout": 1.2, "neutral": 1.0},
    "CRASH":      {"trend": 1.3, "range": 0.5, "breakout": 1.2, "neutral": 1.0},
    "RANGE":      {"trend": 0.5, "range": 1.3, "breakout": 0.7, "neutral": 1.0},
    "VOLATILE":   {"trend": 0.8, "range": 0.6, "breakout": 1.5, "neutral": 1.0},
}


def get_regime_multiplier(strategy_type: str, current_regime: str) -> float:
    """Return capital weight multiplier for a strategy type in the current regime.

    strategy_type: 'trend' | 'range' | 'breakout' | 'neutral'
    current_regime: one of TREND_UP / TREND_DOWN / MELTUP / CRASH / RANGE / VOLATILE

    Returns a float in [0.5, 1.5]. The caller clamps into [0.25, 2.0] after
    combining with the performance multiplier.
    """
    row = _REGIME_MULTIPLIER_TABLE.get(current_regime.upper())
    if row is None:
        return 1.0
    return row.get(str(strategy_type).lower(), 1.0)
