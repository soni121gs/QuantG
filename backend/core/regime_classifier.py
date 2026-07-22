"""RAE-1 (2026-07-10): the no-lookahead intraday REGIME CLASSIFIER — the new organ
of the Regime-Aware Ensemble (CLAUDE.md §18). Given the bars SO FAR (and optional
market context), it answers "what regime are we in right now, and how sure are we?"
as `(label, confidence)` over the canonical taxonomy (RAE-0, `regime_taxonomy`).

Two hard rules, both from the RES design law:
  1. NO LOOKAHEAD — it reads only `bars[:now]`, never the rest of the day. This is
     what separates it from `regime_taxonomy.classify_day` (which labels a COMPLETED
     day for judging). A live gate/router may only consume THIS.
  2. LIVE == HISTORICAL — it takes a plain list of 1-minute bars (identical shape
     from the live feed buffer or the index-minute store) plus an optional context
     dict, so the same code classifies a live minute and a backtest minute. That is
     the non-negotiable rule that lets a regime signal be OOS-validated (RAE-2).

Confidence ∈ [0,1] and is MATURITY-aware: early in the session a trend is not yet
established, so confidence is scaled down until enough bars have printed. This is
the honest version of "is this a trend day" — the 498-day study showed a loose,
over-confident trend read is what turns a real edge into a loss (range fakeouts),
so the classifier is deliberately reluctant to call TREND early.

Reuses (not duplicates): `regime_taxonomy` thresholds/labels, and — when a context
is supplied — `market_context.vol_state`/`event_context`. Pure; no I/O.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from core import regime_taxonomy as tax


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


OR_MINUTES = int(_f("RAE_OR_MINUTES", 15))              # opening range = first N bars
MIN_BARS_CONFIDENT = int(_f("RAE_MIN_BARS_CONFIDENT", 45))  # trend needs ~45m to earn full confidence
RANGE_BASE_CONF = _f("RAE_RANGE_BASE_CONF", 0.40)       # default-state confidence


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class RegimeSnapshot:
    label: str
    confidence: float
    features: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"label": self.label, "confidence": round(self.confidence, 3),
                "features": {k: round(v, 4) for k, v in self.features.items()},
                "reasons": list(self.reasons)}


def _clock(bar: Dict[str, Any]) -> str:
    return str(bar.get("date") or bar.get("timestamp_ist") or "")[11:16]


def _intraday_features(bars: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """Features from bars SO FAR only (no lookahead)."""
    o = float(bars[0]["open"])
    cur = float(bars[-1]["close"])
    hi = max(float(b["high"]) for b in bars)
    lo = min(float(b["low"]) for b in bars)
    rng = hi - lo
    path = sum(abs(float(bars[i]["close"]) - float(bars[i - 1]["close"]))
               for i in range(1, len(bars)))
    or_n = bars[:OR_MINUTES]
    or_hi = max(float(b["high"]) for b in or_n) if or_n else hi
    or_lo = min(float(b["low"]) for b in or_n) if or_n else lo
    # running VWAP (typical price; volume-weighted, falls back to mean when index
    # spot volume is 0) + slope over the last 10 bars
    cum_pv = cum_v = 0.0
    vwaps: List[float] = []
    for b in bars:
        tp = (float(b["high"]) + float(b["low"]) + float(b["close"])) / 3.0
        v = float(b.get("volume") or 0) or 1.0
        cum_pv += tp * v
        cum_v += v
        vwaps.append(cum_pv / cum_v)
    vwap_slope = vwaps[-1] - vwaps[-11] if len(vwaps) > 11 else 0.0
    return {
        "ret_pct": (cur - o) / o * 100.0 if o else 0.0,
        "rng_pct": rng / o * 100.0 if o else 0.0,
        "close_loc": (cur - lo) / rng if rng > 0 else 0.5,
        "efficiency": abs(cur - o) / path if path > 0 else 0.0,
        "cur": cur, "or_hi": or_hi, "or_lo": or_lo,
        "vwap": vwaps[-1], "vwap_slope": vwap_slope,
        "bars": float(len(bars)),
    }


def classify_intraday(
    bars: Sequence[Dict[str, Any]],
    *,
    context: Optional[Dict[str, Any]] = None,
    is_event: bool = False,
) -> RegimeSnapshot:
    """Classify the current regime from bars SO FAR. `context` (optional) is a
    `market_context.build_context()`-shaped dict; when present its vol_state and
    event flags refine the label/confidence, but the classifier works price-only."""
    reasons: List[str] = []

    # EVENT is an overlay — a known event day is EVENT regardless of price shape,
    # because the router caps/skips risk on it either way.
    ev = bool(is_event)
    if context is not None:
        ec = context.get("event_context") or context.get("events") or {}
        ev = ev or bool(ec.get("is_event") or ec.get("is_expiry"))
    if ev:
        return RegimeSnapshot(tax.EVENT, 0.9, {}, ["event/expiry day — stand-down overlay"])

    if not bars or len(bars) < 3:
        return RegimeSnapshot(tax.RANGE, 0.1, {}, ["too few bars — default RANGE, low confidence"])

    f = _intraday_features(bars)
    # maturity: a trend/chop read is only trustworthy once enough bars have printed
    maturity = _clamp(f["bars"] / MIN_BARS_CONFIDENT)
    broke_up = f["cur"] > f["or_hi"]
    broke_dn = f["cur"] < f["or_lo"]
    calm_bonus = 0.0
    if context is not None:
        vs = (context.get("vol_state") or {}).get("state")
        if vs == "CALM":
            calm_bonus = 0.10   # trends ride low/falling vol
        elif vs == "STORMY":
            calm_bonus = -0.10

    # ---- TREND_UP ----
    if (f["ret_pct"] >= tax.TREND_RET_PCT and f["close_loc"] >= tax.TREND_CLOSE_LOC
            and f["efficiency"] >= tax.TREND_EFFICIENCY and broke_up and f["vwap_slope"] > 0):
        strength = (_clamp(f["ret_pct"] / (2 * tax.TREND_RET_PCT))
                    + _clamp(f["efficiency"] / (2 * tax.TREND_EFFICIENCY))
                    + _clamp(f["close_loc"])) / 3.0
        conf = _clamp(strength * maturity + calm_bonus)
        reasons.append(f"up {f['ret_pct']:.2f}% eff {f['efficiency']:.2f} broke OR-high, vwap rising")
        return RegimeSnapshot(tax.TREND_UP, conf, f, reasons)

    # ---- TREND_DOWN ----
    if (f["ret_pct"] <= -tax.TREND_RET_PCT and f["close_loc"] <= (1.0 - tax.TREND_CLOSE_LOC)
            and f["efficiency"] >= tax.TREND_EFFICIENCY and broke_dn and f["vwap_slope"] < 0):
        strength = (_clamp(-f["ret_pct"] / (2 * tax.TREND_RET_PCT))
                    + _clamp(f["efficiency"] / (2 * tax.TREND_EFFICIENCY))
                    + _clamp(1.0 - f["close_loc"])) / 3.0
        conf = _clamp(strength * maturity + calm_bonus)
        reasons.append(f"down {f['ret_pct']:.2f}% eff {f['efficiency']:.2f} broke OR-low, vwap falling")
        return RegimeSnapshot(tax.TREND_DOWN, conf, f, reasons)

    # ---- HIGH_VOL_CHOP ----  big range so far, no efficient direction
    if f["rng_pct"] >= tax.CHOP_RANGE_PCT and f["efficiency"] < tax.TREND_EFFICIENCY:
        conf = _clamp(_clamp(f["rng_pct"] / (2 * tax.CHOP_RANGE_PCT)) * maturity - calm_bonus)
        reasons.append(f"wide range {f['rng_pct']:.2f}% but low efficiency {f['efficiency']:.2f} — whipsaw")
        return RegimeSnapshot(tax.HIGH_VOL_CHOP, conf, f, reasons)

    # ---- INSIDE_QUIET ----  very tight range so far
    if f["rng_pct"] < tax.INSIDE_RANGE_PCT:
        conf = _clamp((tax.INSIDE_RANGE_PCT - f["rng_pct"]) / tax.INSIDE_RANGE_PCT * maturity + 0.2)
        reasons.append(f"tight range {f['rng_pct']:.2f}% — inside/quiet")
        return RegimeSnapshot(tax.INSIDE_QUIET, conf, f, reasons)

    # ---- RANGE (default) ----
    # RANGE is the fall-through: "no decisive signature YET". Every other branch
    # scales its confidence by maturity; this one used to return a flat 0.40, so an
    # immature session was indistinguishable from a genuinely established range.
    # That is not a cosmetic gap — RANGE is the premium sellers' home regime, so a
    # confident-looking default meant "we don't know yet" was routed as "green
    # light, full size". Measured 2026-07-22: every entry that day stamped
    # RANGE/0.40 while the mature coarse regime read TREND_DOWN, and the sellers
    # lost ₹2.2k selling puts into it. Scale it like everything else.
    conf = _clamp(RANGE_BASE_CONF * maturity)
    reasons.append(
        f"no decisive trend/chop/inside signature — default RANGE "
        f"(maturity {maturity:.2f} over {int(f['bars'])} bars)"
    )
    return RegimeSnapshot(tax.RANGE, conf, f, reasons)


def classify_at(day_bars: Sequence[Dict[str, Any]], cutoff_hhmm: str,
                *, context: Optional[Dict[str, Any]] = None,
                is_event: bool = False) -> RegimeSnapshot:
    """Classify a historical day using only bars up to `cutoff_hhmm` IST (no
    lookahead) — the entry point the OOS backtester/router uses to ask 'what did
    the classifier believe at 11:30 that day?'. Empty if no bars before cutoff."""
    upto = [b for b in day_bars if _clock(b) and _clock(b) <= cutoff_hhmm]
    if not upto:
        return RegimeSnapshot(tax.RANGE, 0.0, {}, ["no bars before cutoff"])
    return classify_intraday(upto, context=context, is_event=is_event)
