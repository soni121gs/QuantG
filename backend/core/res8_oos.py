"""RES-8 — the OOS gate: grade the seller brain AND an IV-cheap buyer variant
on the SAME real data, side by side.

Reconstructs a DAILY market_context from the free bhavcopy store (item G — the
historical adapter), turns it into entry signals, and grades them through the
same settlement-priced EOD engine + walk-forward verdict the rest of the book
uses. Realistic slippage is already modelled per leg (RES-1 discipline).

Two variants off the SAME signal engine:
  • SELLER  — RES-7's gate: premium RICH (IV−RV ≥ min) + RANGE + not stormy →
              sell a width-1 credit spread (bull-put / bear-call by bias).
  • BUYER   — the honest inverse: premium CHEAP (IV−RV ≤ −min) + a trending
              regime → buy a debit spread in the trend direction.

HONEST LIMITATION (same as CLAUDE.md §13.3): this is DAILY settle-to-settle held
to theta on weekly options — it validates the SIGNAL + structure, not the
intraday minute-perfect scalp exit (that's the IMD judge, which is data-thin).
The regime here is a daily proxy (close vs SMA20 + 5-day drift), not the
intraday VWAP regime. IV is proxied from the ATM straddle (no VIX in the store).
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from core.bhavcopy_store import BhavcopyStore
from core.eod_options_backtest import (
    EODOptionsBacktest, walk_forward, _pick_expiry, _dte,
    MIN_DTE_DAYS, MAX_DTE_DAYS,
)
from core.market_context import vol_edge, vol_state


def _iv_proxy(store: BhavcopyStore, u: str, day: str, spot: float) -> Optional[float]:
    """Annualised IV (%) backed out of the ATM straddle: straddle ≈ 0.8·S·σ·√(T),
    so σ = straddle / (0.8·S·√(dte/252)). A clean IV read from settle prices with
    no VIX dependency."""
    expiry = _pick_expiry(store, u, day, MIN_DTE_DAYS, MAX_DTE_DAYS)
    if not expiry:
        return None
    chain = store.option_chain(u, day).get(expiry, {})
    if not chain:
        return None
    strikes = list(chain.keys())
    atm = min(strikes, key=lambda k: abs(k - spot))
    ce = store.leg_settle(u, day, expiry, atm, "CE")
    pe = store.leg_settle(u, day, expiry, atm, "PE")
    if not ce or not pe:
        return None
    dte = max(1, _dte(day, expiry))
    straddle = float(ce) + float(pe)
    denom = 0.8 * float(spot) * math.sqrt(dte / 252.0)
    if denom <= 0:
        return None
    return (straddle / denom) * 100.0


def _daily_regime(closes: List[float]) -> tuple:
    """Daily-granularity regime proxy → (regime, bias). Close vs SMA20 + 5-day
    drift. A stand-in for the intraday VWAP regime the live engine uses."""
    if len(closes) < 21:
        return ("RANGE", "NEUTRAL")
    close = closes[-1]
    sma20 = sum(closes[-20:]) / 20.0
    ret5 = (close - closes[-6]) / closes[-6] if closes[-6] else 0.0
    if close > sma20 and ret5 > 0.01:
        return ("TREND_UP", "BULL")
    if close < sma20 and ret5 < -0.01:
        return ("TREND_DOWN", "BEAR")
    return ("RANGE", "BULL" if close > sma20 else "BEAR")


def build_signals(store: BhavcopyStore, u: str, start: str, end: str,
                  mode: str, min_edge: float = 2.0) -> List[Dict[str, Any]]:
    """Reconstruct daily signals for `mode` in {"seller","buyer"}. Signal action:
    BUY = bullish (sell PE / buy CE), SELL = bearish (sell CE / buy PE) — the
    convention EODOptionsBacktest._open expects."""
    candles = store.underlying_daily(u, start, end)
    closes = [float(c["close"]) for c in candles]
    sigs: List[Dict[str, Any]] = []
    for i, c in enumerate(candles):
        if i < 21:
            continue
        day = str(c["date"])[:10]
        spot = float(c["close"])
        window = closes[: i + 1]
        iv = _iv_proxy(store, u, day, spot)
        if iv is None:
            continue
        ve = vol_edge(iv, window, min_points=min_edge)
        vs = vol_state(window)
        regime, bias = _daily_regime(window)
        if mode == "seller":
            if ve.get("rich") and regime == "RANGE" and vs.get("state") != "STORMY":
                # RANGE + rich → sell the side the tape drifts toward.
                sigs.append({"date": day, "action": "BUY" if bias == "BULL" else "SELL",
                             "iv": round(iv, 1), "edge": ve.get("edge")})
        else:  # buyer: IV cheap + trending → buy in the trend direction
            edge = ve.get("edge")
            if edge is not None and edge <= -float(min_edge) and regime in ("TREND_UP", "TREND_DOWN"):
                sigs.append({"date": day, "action": "BUY" if regime == "TREND_UP" else "SELL",
                             "iv": round(iv, 1), "edge": edge})
    return sigs


def run_res8(u: str = "NIFTY", start: str = "2024-01-01", end: str = "2025-12-31",
             min_edge: float = 2.0) -> Dict[str, Any]:
    """Grade the seller and the IV-cheap buyer on the same window. Returns both
    verdicts side by side."""
    store = BhavcopyStore()
    bt = EODOptionsBacktest(store)

    def _grade(mode: str, structure: str, params: Dict[str, Any]) -> Dict[str, Any]:
        sigs = build_signals(store, u, start, end, mode, min_edge)
        strat = {"id": f"res8_{mode}", "name": f"{u} {mode} (RES-8)",
                 "visual_config": {"options": {"underlying": u, "structure": structure}}}
        res = bt.run(strat, start, end, params=params, signals=sigs)
        if "error" in res:
            return {"n_signals": len(sigs), "verdict": "ERROR", "error": res["error"]}
        wf = walk_forward(res)
        return {"n_signals": len(sigs), "n_trades": res.get("total_trades") or len(res.get("trades") or []),
                "verdict": wf["verdict"], "overall": wf["overall"], "oos": wf.get("oos"),
                "oos_year": wf.get("oos_year"), "pct_green_months": wf.get("pct_green_months"),
                "all_years_positive": wf.get("all_years_positive"), "by_year": wf.get("by_year")}

    seller = _grade("seller", "credit_spread",
                    {"width": 1, "short_otm_pct": 0.0, "credit_tp": 0.35, "credit_sl": 1.5,
                     "min_dte": 2, "max_dte": 7, "max_hold_days": 5})
    buyer = _grade("buyer", "debit_spread",
                   {"width": 2, "min_dte": 2, "max_dte": 7, "max_hold_days": 5})
    return {"underlying": u, "window": {"start": start, "end": end}, "min_edge": min_edge,
            "seller": seller, "buyer": buyer}
