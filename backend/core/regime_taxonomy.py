"""RAE-0 (2026-07-10): the canonical market-regime taxonomy for the Regime-Aware
Ensemble (CLAUDE.md §18). This is the single source of truth for "what kind of day
is it" — every downstream RAE block (the classifier RAE-1, the regime-conditional
judge RAE-2, the specialists, the router RAE-4) keys off THESE labels and THESE
thresholds. Freeze here; do not redefine regime labels elsewhere.

Derived from the 498-day real NIFTY 1-minute study (`scratch/regime_directional_oos.py`,
2026-07-10). The finding that forces the ensemble: the market is a regime machine
and each regime has a different winner —

    RANGE          ~60%   choppy, small net move            -> premium sellers
    INSIDE_QUIET   ~26%   tight range                       -> sellers + mean-revert
    HIGH_VOL_CHOP  ~13%   big range, no net direction        -> STAND DOWN (nothing wins)
    TREND_UP/DOWN   ~1%   directional, efficient             -> delta-1 directional
    EVENT           (overlay)  expiry / macro / results       -> stand down / defined-risk

`classify_day()` here is the DESCRIPTIVE, full-day labeler used to bucket a COMPLETED
day for regime-conditional judging (RAE-2) — it legitimately uses the whole day's
bars because it labels an outcome, not a live decision. The no-lookahead, intraday,
confidence-scored classifier a live gate/router consumes is RAE-1
(`core/regime_classifier.py`), which imports the thresholds from here.

Pure + env-tunable (RAE_* overrides). No I/O, no broker, no DB.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Sequence

# ---- canonical labels (freeze) ---------------------------------------------
TREND_UP = "TREND_UP"
TREND_DOWN = "TREND_DOWN"
RANGE = "RANGE"
HIGH_VOL_CHOP = "HIGH_VOL_CHOP"
INSIDE_QUIET = "INSIDE_QUIET"
EVENT = "EVENT"

REGIME_LABELS = (TREND_UP, TREND_DOWN, RANGE, HIGH_VOL_CHOP, INSIDE_QUIET, EVENT)

# Which specialist owns each regime (RAE-3x). EVENT is an overlay handled by the
# router (stand down / defined-risk) regardless of the price-derived label.
REGIME_OWNER = {
    TREND_UP: "trend_delta1_long",
    TREND_DOWN: "trend_delta1_short",
    RANGE: "range_seller",
    INSIDE_QUIET: "inside_mean_revert",   # sellers also participate
    HIGH_VOL_CHOP: "stand_down",
    EVENT: "stand_down",
}


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---- thresholds (from the 498-day study; env-tunable) ----------------------
# A day is TRENDING when it moved decisively, closed near the extreme it trended
# toward, and traveled EFFICIENTLY (net move / total path — an efficiency ratio).
TREND_RET_PCT = _f("RAE_TREND_RET_PCT", 0.35)          # |open->close| %
TREND_CLOSE_LOC = _f("RAE_TREND_CLOSE_LOC", 0.60)      # close in top/bottom 40% of range
TREND_EFFICIENCY = _f("RAE_TREND_EFFICIENCY", 0.18)    # net/path
# Big total range with LOW efficiency = whipsaw (no one wins).
CHOP_RANGE_PCT = _f("RAE_CHOP_RANGE_PCT", 1.40)        # day range as % of open
# Tight total range = inside/quiet (theta heaven).
INSIDE_RANGE_PCT = _f("RAE_INSIDE_RANGE_PCT", 0.60)

# Observed base rates on the 498-day NIFTY sample (2024-01..2025-12). Documented
# so downstream code and reports can sanity-check the live label mix vs history.
BASE_RATES = {
    RANGE: 0.60,
    INSIDE_QUIET: 0.26,
    HIGH_VOL_CHOP: 0.13,
    TREND_UP: 0.012,
    TREND_DOWN: 0.012,
}


def day_features(bars: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """Full-day descriptive features from a completed day's 1-minute bars."""
    o = float(bars[0]["open"])
    c = float(bars[-1]["close"])
    hi = max(float(b["high"]) for b in bars)
    lo = min(float(b["low"]) for b in bars)
    rng = hi - lo
    path = sum(abs(float(bars[i]["close"]) - float(bars[i - 1]["close"]))
               for i in range(1, len(bars)))
    return {
        "ret_pct": (c - o) / o * 100.0 if o else 0.0,
        "rng_pct": rng / o * 100.0 if o else 0.0,
        "close_loc": (c - lo) / rng if rng > 0 else 0.5,   # 1=closed at high, 0=at low
        "efficiency": abs(c - o) / path if path > 0 else 0.0,
    }


def classify_day(bars: Sequence[Dict[str, Any]], *, is_event: bool = False) -> str:
    """Label a COMPLETED day (descriptive, full-day — for RAE-2 bucketing). EVENT is
    an overlay: an event day is labeled EVENT regardless of price shape, because the
    router stands down / caps risk on it either way."""
    if is_event:
        return EVENT
    if len(bars) < 2:
        return RANGE
    f = day_features(bars)
    if f["ret_pct"] >= TREND_RET_PCT and f["close_loc"] >= TREND_CLOSE_LOC and f["efficiency"] >= TREND_EFFICIENCY:
        return TREND_UP
    if f["ret_pct"] <= -TREND_RET_PCT and f["close_loc"] <= (1.0 - TREND_CLOSE_LOC) and f["efficiency"] >= TREND_EFFICIENCY:
        return TREND_DOWN
    if f["rng_pct"] >= CHOP_RANGE_PCT and f["efficiency"] < TREND_EFFICIENCY:
        return HIGH_VOL_CHOP
    if f["rng_pct"] < INSIDE_RANGE_PCT:
        return INSIDE_QUIET
    return RANGE


def base_rate(label: str) -> float:
    return BASE_RATES.get(label, 0.0)
