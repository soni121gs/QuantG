"""RES-2 — Market Intelligence Engine (the signal layer).

Produces ONE `market_context` snapshot bundling the five Phase-1 intelligence
inputs, computed by PURE functions so the SAME code serves both the live feed
and the historical backtester (item G — a signal that can't be reconstructed
historically can never be OOS-validated). This module owns no I/O and no DB; the
live adapter (strategy_runner) and the historical adapter (the OOS backtester)
each hand it plain data and read back the same shape.

The five inputs (CLAUDE.md §15.3):
  A) IV − realized_vol   — THE edge. Sell only when implied vol is expensive vs
                           delivered vol, and the gap is fat.
  B) Regime + vol_state  — gate + side-picker. Seller edge lives in RANGE, dies
                           in TREND. Reuses market_regime.compute_regime_from_data.
  C) Chain intelligence  — OI walls, PCR, skew → which side is safer to sell.
  D) Order-flow imbalance— ENTRY FILTER only (no latency edge on a ₹5k VPS).
                           Reuses order_flow.orderflow_imbalance.
  E) Event calendar      — fat-tail gate; don't sell premium into a known event.

`build_context(...)` synthesises them into a `seller_decision` — the single
answer the scalper (RES-7) and the OOS judge (RES-8) both consume.
"""
from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from market_regime import compute_regime_from_data
from order_flow import orderflow_imbalance

# ── Config (env-overridable) ────────────────────────────────────────────────────
# A: minimum IV−RV gap (annualised vol points) for premium to count as "rich".
VOL_EDGE_MIN_POINTS = float(os.environ.get("VOL_EDGE_MIN_POINTS", "2.0"))
# B: vol-state bands — short/long realised-vol ratio and absolute short RV (%).
VOL_STATE_STORMY_RATIO = float(os.environ.get("VOL_STATE_STORMY_RATIO", "1.4"))
VOL_STATE_STORMY_ABS = float(os.environ.get("VOL_STATE_STORMY_ABS", "24.0"))
VOL_STATE_CALM_RATIO = float(os.environ.get("VOL_STATE_CALM_RATIO", "0.8"))
VOL_STATE_CALM_ABS = float(os.environ.get("VOL_STATE_CALM_ABS", "10.0"))
RV_SHORT_DAYS = int(os.environ.get("VOL_RV_SHORT_DAYS", "5"))
RV_LONG_DAYS = int(os.environ.get("VOL_RV_LONG_DAYS", "20"))
TRADING_DAYS_PER_YEAR = 252.0

# Regimes in which a premium SELLER is allowed to open at all.
SELLER_OK_REGIMES = {"RANGE"}


# ── A) Volatility edge: IV − realized_vol ───────────────────────────────────────

def realized_vol_pct(closes: Sequence[float], window: Optional[int] = None) -> Optional[float]:
    """Annualised realised volatility (%) from a series of closes (daily bars,
    oldest→newest). Uses log returns; None if fewer than 2 usable closes."""
    vals = [float(c) for c in closes if c and float(c) > 0]
    if window is not None and window > 0:
        vals = vals[-(window + 1):]  # +1 close → `window` returns
    if len(vals) < 2:
        return None
    rets = [math.log(vals[i] / vals[i - 1]) for i in range(1, len(vals))]
    n = len(rets)
    if n < 2:
        return None
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0


def vol_edge(iv_pct: Optional[float], daily_closes: Sequence[float],
             min_points: float = VOL_EDGE_MIN_POINTS) -> Dict[str, Any]:
    """A) The edge signal. iv_pct = current implied vol in annualised % (India
    VIX for NIFTY). Returns iv, rv, edge (iv−rv), and `rich` = the gap is
    positive AND at least `min_points` fat. `rich` is None when data is missing
    (unknown, never a flattering True)."""
    rv = realized_vol_pct(daily_closes, window=RV_LONG_DAYS)
    if iv_pct is None or rv is None:
        return {"iv": iv_pct, "rv": rv, "edge": None, "rich": None,
                "reason": "vol_edge: missing iv or realized vol"}
    edge = round(float(iv_pct) - rv, 2)
    rich = edge >= float(min_points)
    return {"iv": round(float(iv_pct), 2), "rv": round(rv, 2), "edge": edge,
            "rich": rich,
            "reason": (f"IV {iv_pct:.1f} − RV {rv:.1f} = {edge:+.1f}vol pts "
                       f"({'RICH' if rich else 'thin'}, min {min_points})")}


# ── B) Volatility state (calm / normal / stormy) ────────────────────────────────

def vol_state(daily_closes: Sequence[float]) -> Dict[str, Any]:
    """Is volatility expanding? Short-window RV vs long-window RV + absolute
    level → CALM / NORMAL / STORMY. Sellers avoid STORMY (vol expansion is when
    a range breaks and a sold spread runs to max loss)."""
    rv_short = realized_vol_pct(daily_closes, window=RV_SHORT_DAYS)
    rv_long = realized_vol_pct(daily_closes, window=RV_LONG_DAYS)
    if rv_short is None or rv_long is None or rv_long <= 0:
        return {"state": "UNKNOWN", "rv_short": rv_short, "rv_long": rv_long, "ratio": None}
    ratio = rv_short / rv_long
    if ratio >= VOL_STATE_STORMY_RATIO or rv_short >= VOL_STATE_STORMY_ABS:
        state = "STORMY"
    elif ratio <= VOL_STATE_CALM_RATIO and rv_short <= VOL_STATE_CALM_ABS:
        state = "CALM"
    else:
        state = "NORMAL"
    return {"state": state, "rv_short": round(rv_short, 2),
            "rv_long": round(rv_long, 2), "ratio": round(ratio, 2)}


# ── C) Option-chain intelligence: OI walls, PCR, skew ───────────────────────────

def chain_intel(chain: Sequence[Dict[str, Any]], spot: Optional[float]) -> Dict[str, Any]:
    """From a chain snapshot → PCR, OI walls (max-OI strikes = support/
    resistance), and IV skew (avg OTM put IV − avg OTM call IV). `richer_side`
    is the side carrying more premium/fear. Rows: {strike, option_type CE/PE,
    oi, iv}. Robust to missing fields; returns Nones when the chain is empty."""
    calls = [r for r in chain if str(r.get("option_type", "")).upper() == "CE"]
    puts = [r for r in chain if str(r.get("option_type", "")).upper() == "PE"]

    def _sum_oi(rows):
        return sum(float(r.get("oi") or 0) for r in rows)

    def _max_oi_strike(rows):
        best, best_oi = None, -1.0
        for r in rows:
            oi = float(r.get("oi") or 0)
            if oi > best_oi and r.get("strike") is not None:
                best, best_oi = float(r["strike"]), oi
        return best

    call_oi, put_oi = _sum_oi(calls), _sum_oi(puts)
    pcr = round(put_oi / call_oi, 3) if call_oi > 0 else None
    call_wall = _max_oi_strike(calls)   # resistance
    put_wall = _max_oi_strike(puts)     # support

    skew = None
    richer_side = None
    if spot and spot > 0:
        def _avg_otm_iv(rows, otm_pred):
            ivs = [float(r["iv"]) for r in rows
                   if r.get("iv") not in (None, "") and r.get("strike") is not None
                   and otm_pred(float(r["strike"]))]
            return sum(ivs) / len(ivs) if ivs else None
        put_iv = _avg_otm_iv(puts, lambda k: k < spot)   # OTM puts below spot
        call_iv = _avg_otm_iv(calls, lambda k: k > spot)  # OTM calls above spot
        if put_iv is not None and call_iv is not None:
            skew = round(put_iv - call_iv, 2)
            richer_side = "PE" if skew > 0 else ("CE" if skew < 0 else None)

    return {"pcr": pcr, "call_wall": call_wall, "put_wall": put_wall,
            "skew": skew, "richer_side": richer_side,
            "n_calls": len(calls), "n_puts": len(puts)}


# ── E) Event calendar: fat-tail gate ────────────────────────────────────────────

def event_context(date_str: str, *, expiry_dates: Optional[Sequence[str]] = None,
                  event_dates: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """E) Don't sell premium into a known event. `date_str` = 'YYYY-MM-DD'.
    `expiry_dates` and `event_dates` (RBI/Fed/CPI/results) are passed in so the
    live adapter reads them from config/DB and the backtester reads them from a
    fixture — same function, same answer. block_new_sells fires on either."""
    d = str(date_str)[:10]
    is_expiry = bool(expiry_dates and d in {str(x)[:10] for x in expiry_dates})
    is_event = bool(event_dates and d in {str(x)[:10] for x in event_dates})
    block = is_expiry or is_event
    return {"date": d, "is_expiry": is_expiry, "is_event": is_event,
            "block_new_sells": block,
            "reason": ("EVENT: expiry/macro day — no new premium sells" if block else None)}


# ── F) The bundle + seller synthesis ────────────────────────────────────────────

def _pick_sell_side(regime: Dict[str, Any], chain: Dict[str, Any]) -> Optional[str]:
    """Which spread to sell in a range. Follow the mild drift: bias BULL → sell a
    PUT spread (bull-put); BEAR → sell a CALL spread (bear-call). On a truly
    neutral bias fall back to the richer side by skew."""
    bias = str(regime.get("bias") or "NEUTRAL").upper()
    if bias == "BULL":
        return "PE"
    if bias == "BEAR":
        return "CE"
    return chain.get("richer_side")


def build_context(
    *,
    index: str,
    candles: List[Dict[str, Any]],
    daily_closes: Sequence[float],
    iv_pct: Optional[float],
    spot: Optional[float] = None,
    chain: Optional[Sequence[Dict[str, Any]]] = None,
    contract: Optional[Dict[str, Any]] = None,
    iv_surface: Optional[Dict[str, Any]] = None,
    date_str: Optional[str] = None,
    expiry_dates: Optional[Sequence[str]] = None,
    event_dates: Optional[Sequence[str]] = None,
    source: str = "live",
) -> Dict[str, Any]:
    """Assemble the full market_context and a seller_decision. PURE — hand it
    live-feed data or historical-store data, get the identical shape back.

    seller_decision.allow_sell is True only when ALL hold:
      • vol_edge.rich   (premium expensive vs delivered)   [A]
      • regime in SELLER_OK_REGIMES (RANGE)                 [B]
      • vol_state != STORMY                                 [B]
      • event.block_new_sells is False                      [E]
    Then side is picked from regime bias + chain skew       [C].
    Order-flow [D] is attached as a filter the caller applies at entry time.
    """
    regime = compute_regime_from_data(index, candles or [])
    ve = vol_edge(iv_pct, daily_closes)
    vs = vol_state(daily_closes)
    ci = chain_intel(chain or [], spot)
    ev = event_context(date_str or (candles[-1].get("date") if candles else "") or "",
                       expiry_dates=expiry_dates, event_dates=event_dates)
    imbalance = orderflow_imbalance(contract or {}) if contract else None
    surface_richness = (iv_surface or {}).get("richness") or {}

    reasons: List[str] = []
    allow = True
    if ve.get("rich") is not True:
        allow = False
        reasons.append(ve.get("reason") or "vol edge not rich")
    if regime.get("regime") not in SELLER_OK_REGIMES:
        allow = False
        reasons.append(f"regime {regime.get('regime')} not sell-safe (need RANGE)")
    if vs.get("state") == "STORMY":
        allow = False
        reasons.append("vol_state STORMY — range likely to break")
    if ev.get("block_new_sells"):
        allow = False
        reasons.append(ev.get("reason") or "event day")

    side = _pick_sell_side(regime, ci) if allow else None
    if allow and side is None:
        allow = False
        reasons.append("no clear sell side (neutral bias, flat skew)")

    return {
        "index": index,
        "source": source,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "regime": regime,
        "vol_edge": ve,
        "iv_surface": {
            "available": bool((iv_surface or {}).get("available")),
            "atm_iv": (iv_surface or {}).get("atm_iv"),
            "near_expiry": ((iv_surface or {}).get("summary") or {}).get("near_expiry"),
            "put_call_skew": ((iv_surface or {}).get("summary") or {}).get("put_call_skew"),
            "richness": surface_richness,
        },
        "vol_state": vs,
        "chain": ci,
        "event": ev,
        "order_flow": {"imbalance": None if imbalance is None else round(imbalance, 3)},
        "seller_decision": {
            "allow_sell": allow,
            "side": side,                       # "PE" (bull-put) / "CE" (bear-call) / None
            "reasons": reasons or ["all gates passed"],
        },
    }
