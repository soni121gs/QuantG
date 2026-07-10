"""RAE-3 (2026-07-10): the specialist library — one strategy per regime, each of
which OWNS one regime and STANDS DOWN outside it (CLAUDE.md §18.3). Every specialist
gates its entries on the RAE-1 classifier (`classify_intraday`), so "whose day is it"
is decided by one shared brain, not per-strategy intuition.

Delta-1 specialists (trend / mean-revert) express their view on a DELTA-1 vehicle
(deep-ITM option / index future): ~1.0 delta, ~zero theta — the fix for why every
OTM-option directional strategy died (theta ate the move). P&L = index points × lot,
minus honest costs (slippage + brokerage). These are PURE (a day's bars in → pnl or
None out), so the same code runs in the OOS judge (RAE-2) now and the live router
(RAE-4) later.

  RAE-3a  chop_stand_down       HIGH_VOL_CHOP / EVENT → don't trade (risk cut)
  RAE-3b  inside_mean_revert    INSIDE_QUIET          → fade to VWAP
  RAE-3c  trend_long / _short   TREND_UP / TREND_DOWN → delta-1 with the confidence
                                                        + daily-alignment gate (precision)
  RAE-3d  range sellers          RANGE                 → the EXISTING option-selling book
                                                        (QG-O11 etc., OOS-validated separately;
                                                        option-priced, not delta-1 — owned here
                                                        by REGIME_OWNER, gated live at RAE-4)
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from core import regime_taxonomy as tax
from core.regime_classifier import classify_intraday, RegimeSnapshot


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


LOT = _f("RAE_LOT", 65)                 # NIFTY; delta-1 => 1 index pt = LOT rupees
SLIPPAGE_PTS = _f("RAE_SLIPPAGE_PTS", 2.0)  # per side, conservative for deep-ITM/future
BROKER_RT = _f("RAE_BROKER_RT", 40.0)   # flat brokerage per lot, round trip
OR_MINUTES = int(_f("RAE_OR_MINUTES", 15))
ENTRY_START, ENTRY_STOP = "09:30", "14:00"
SQUAREOFF = "15:20"
TREND_CONF_MIN = _f("RAE_TREND_CONF_MIN", 0.90)   # only fire trend on high-confidence calls


def _clock(bar: Dict[str, Any]) -> str:
    return str(bar.get("date") or bar.get("timestamp_ist") or "")[11:16]


def _atr(highs, lows, closes, period=14) -> float:
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if not trs:
        return 0.0
    w = trs[-period:]
    return sum(w) / len(w)


def _cost() -> float:
    return (2 * SLIPPAGE_PTS) * LOT + BROKER_RT


# ---- RAE-3a: stand-down ----------------------------------------------------
def chop_stand_down(snapshot: RegimeSnapshot) -> bool:
    """True when the book should NOT trade — HIGH_VOL_CHOP (nothing wins) or EVENT.
    The cheapest edge in the study: not bleeding on the 13% whipsaw days."""
    return snapshot.label in (tax.HIGH_VOL_CHOP, tax.EVENT)


# ---- RAE-3c: trend delta-1 -------------------------------------------------
def trend_delta1(
    day_bars: Sequence[Dict[str, Any]],
    side: int,
    *,
    daily_trend_up: Optional[bool] = None,
    conf_min: float = TREND_CONF_MIN,
    cfg: Optional[Dict[str, float]] = None,
) -> Optional[float]:
    """One delta-1 trend trade. side=+1 long (TREND_UP) / -1 short (TREND_DOWN).
    Entry gate = RAE-1 says the matching TREND label AT high confidence, PLUS
    higher-timeframe (daily) alignment when supplied — the precision levers the
    498-day study showed are required (a bare intraday trend call is 16% precise).
    ATR trailing exit, EOD square-off. Returns pnl or None (stood down)."""
    cfg = cfg or {"stop_atr": 1.2, "trail_atr": 2.5}
    want = tax.TREND_UP if side > 0 else tax.TREND_DOWN
    if daily_trend_up is not None:
        aligned = daily_trend_up if side > 0 else (not daily_trend_up)
        if not aligned:
            return None   # daily trend disagrees — stand down
    if len(day_bars) < 30:
        return None
    closes = [float(b["close"]) for b in day_bars]
    highs = [float(b["high"]) for b in day_bars]
    lows = [float(b["low"]) for b in day_bars]

    pos: Optional[Dict[str, float]] = None
    for i in range(OR_MINUTES, len(day_bars)):
        b = day_bars[i]
        clk = _clock(b)
        px = float(b["close"])
        atr = _atr(highs[:i + 1], lows[:i + 1], closes[:i + 1], 14)
        if atr <= 0:
            continue
        if pos is None and ENTRY_START <= clk <= ENTRY_STOP:
            snap = classify_intraday(day_bars[:i + 1])
            if snap.label == want and snap.confidence >= conf_min:
                pos = {"entry": px, "stop": px - side * cfg["stop_atr"] * atr, "peak": px}
            continue
        if pos is not None:
            if side > 0:
                pos["peak"] = max(pos["peak"], px)
                pos["stop"] = max(pos["stop"], pos["peak"] - cfg["trail_atr"] * atr)
                hit = px <= pos["stop"]
            else:
                pos["peak"] = min(pos["peak"], px)
                pos["stop"] = min(pos["stop"], pos["peak"] + cfg["trail_atr"] * atr)
                hit = px >= pos["stop"]
            if hit or clk >= SQUAREOFF or i == len(day_bars) - 1:
                return side * (px - pos["entry"]) * LOT - _cost()
    return None


def trend_long(day_bars, **kw):
    return trend_delta1(day_bars, +1, **kw)


def trend_short(day_bars, **kw):
    return trend_delta1(day_bars, -1, **kw)


# ---- RAE-3b: inside mean-revert --------------------------------------------
def inside_mean_revert(
    day_bars: Sequence[Dict[str, Any]],
    *,
    cfg: Optional[Dict[str, float]] = None,
) -> Optional[float]:
    """Fade a stretch from VWAP back to VWAP — the INSIDE_QUIET / RANGE specialist
    (study: +₹164/day, 51% WR on inside days). Only acts while RAE-1 says the regime
    is INSIDE_QUIET or RANGE (stands down in TREND/CHOP). Delta-1."""
    cfg = cfg or {"fade_atr": 2.0, "stop_atr": 1.5}
    if len(day_bars) < 30:
        return None
    closes = [float(b["close"]) for b in day_bars]
    highs = [float(b["high"]) for b in day_bars]
    lows = [float(b["low"]) for b in day_bars]

    pos: Optional[Dict[str, float]] = None
    for i in range(OR_MINUTES, len(day_bars)):
        b = day_bars[i]
        clk = _clock(b)
        px = float(b["close"])
        atr = _atr(highs[:i + 1], lows[:i + 1], closes[:i + 1], 14)
        if atr <= 0:
            continue
        snap = classify_intraday(day_bars[:i + 1])
        vwap = snap.features.get("vwap", px)
        if pos is None and ENTRY_START <= clk <= ENTRY_STOP:
            if snap.label not in (tax.INSIDE_QUIET, tax.RANGE):
                continue   # stand down outside its regime
            dev = (px - vwap) / atr
            if dev >= cfg["fade_atr"]:
                pos = {"side": -1.0, "entry": px, "stop": px + cfg["stop_atr"] * atr, "tgt": vwap}
            elif dev <= -cfg["fade_atr"]:
                pos = {"side": 1.0, "entry": px, "stop": px - cfg["stop_atr"] * atr, "tgt": vwap}
            continue
        if pos is not None:
            s = pos["side"]
            hit_stop = (px <= pos["stop"]) if s > 0 else (px >= pos["stop"])
            hit_tgt = (px >= pos["tgt"]) if s > 0 else (px <= pos["tgt"])
            if hit_stop or hit_tgt or clk >= SQUAREOFF or i == len(day_bars) - 1:
                return s * (px - pos["entry"]) * LOT - _cost()
    return None
