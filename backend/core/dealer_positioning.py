"""IA-1 (2026-07-11): Dealer positioning / Gamma Exposure (GEX) from the OI + settle
data we ALREADY store (bhavcopy per-strike close/settle/oi/underlying_price/lot).

Why this is the goldmine: per-strike open interest + a Black-Scholes gamma give the
NET gamma the options dealers must hedge. The sign maps straight onto the RAE regime
thesis (CLAUDE.md §18):

  POSITIVE GEX  -> dealers long gamma  -> they sell rallies / buy dips
                -> compressed vol, range-bound, mean-reverting   => RANGE seller's day
  NEGATIVE GEX  -> dealers short gamma -> they buy rallies / sell dips
                -> vol expansion, trends persist                 => TREND specialist's day

So GEX is the precision CONFIRMATION the trend gate (RAE-3c) is missing: a trend call
with negative-GEX backing is far more reliable than a bare price breakout. And it is
fully historically reconstructable from the store (the non-negotiable OOS rule) — every
input (spot, strike, OI, premium, expiry) is in the bhavcopy.

Convention (SqueezeMetrics/SpotGamma naive, matches the §18 mapping): retail is net
long calls / short puts, dealers the opposite; per-strike GEX = call_gamma*call_OI
MINUS put_gamma*put_OI, so the aggregate sign is POSITIVE when dealers dampen and
NEGATIVE when they amplify.

Pure — no I/O. `dealer_state_from_chain()` takes a plain chain dict; `from_store()` is
a thin adapter over BhavcopyStore for live + backtest callers.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# net GEX (in ₹-gamma per 1% move, normalised by 1e7 = ₹1cr) above/below which we
# call the regime POSITIVE / NEGATIVE; between = NEUTRAL. Env-tunable.
GEX_POS_THRESHOLD = _f("GEX_POS_THRESHOLD", 0.0)
GEX_NEG_THRESHOLD = _f("GEX_NEG_THRESHOLD", 0.0)
GEX_SCALE = 1e7  # report net GEX in ₹crore-gamma units so numbers are readable

_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT_2PI


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, sigma: float, is_call: bool, r: float = 0.0) -> float:
    """Black-Scholes price (r≈0 for index options, no dividend)."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if is_call else (K - S))
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_gamma(S: float, K: float, T: float, sigma: float, r: float = 0.0) -> float:
    """Gamma is identical for calls and puts."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def implied_vol(price: float, S: float, K: float, T: float, is_call: bool,
                r: float = 0.0) -> Optional[float]:
    """Invert Black-Scholes for sigma by bisection (bounded, deterministic).
    Returns None when the premium is below intrinsic / unsolvable."""
    if price is None or price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, (S - K) if is_call else (K - S))
    if price < intrinsic - 1e-6:
        return None
    lo, hi = 1e-4, 5.0
    plo = bs_price(S, K, T, lo, is_call, r) - price
    phi = bs_price(S, K, T, hi, is_call, r) - price
    if plo * phi > 0:                       # price outside solvable band
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        pm = bs_price(S, K, T, mid, is_call, r) - price
        if abs(pm) < 1e-6:
            return mid
        if plo * pm <= 0:
            hi, phi = mid, pm
        else:
            lo, plo = mid, pm
    return 0.5 * (lo + hi)


def _year_frac(day: str, expiry: str) -> float:
    try:
        d0 = date.fromisoformat(str(day)[:10])
        d1 = date.fromisoformat(str(expiry)[:10])
        days = max(1, (d1 - d0).days)
    except (TypeError, ValueError):
        days = 1
    return days / 365.0


@dataclass
class DealerState:
    day: str
    underlying: str
    expiry: Optional[str]
    spot: float
    net_gex: float                    # ₹crore-gamma per 1% move (signed)
    gex_state: str                    # POSITIVE | NEGATIVE | NEUTRAL | UNKNOWN
    call_wall: Optional[float] = None  # strike with max call OI (pin above / resistance)
    put_wall: Optional[float] = None   # strike with max put OI (support below)
    zero_gamma: Optional[float] = None  # strike where cumulative GEX flips sign (flip level)
    by_strike: Dict[float, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"day": self.day, "underlying": self.underlying, "expiry": self.expiry,
                "spot": round(self.spot, 2), "net_gex": round(self.net_gex, 3),
                "gex_state": self.gex_state, "call_wall": self.call_wall,
                "put_wall": self.put_wall, "zero_gamma": self.zero_gamma,
                "reasons": self.reasons}


def _state_label(net_gex: float) -> str:
    if net_gex > GEX_POS_THRESHOLD:
        return "POSITIVE"
    if net_gex < GEX_NEG_THRESHOLD:
        return "NEGATIVE"
    return "NEUTRAL"


def dealer_state_from_chain(
    strikes: Dict[float, Dict[str, Dict[str, Any]]],
    spot: float,
    day: str,
    expiry: str,
    *,
    underlying: str = "",
) -> DealerState:
    """Compute GEX + OI walls from ONE expiry's chain.
    `strikes` = {strike: {'CE': row, 'PE': row}} where each row carries close/settle,
    oi, lot_size (raw bhavcopy row shape)."""
    T = _year_frac(day, expiry)
    net = 0.0
    by_strike: Dict[float, float] = {}
    call_oi: Dict[float, float] = {}
    put_oi: Dict[float, float] = {}
    cum: List[tuple] = []

    def _num(row, key):
        try:
            return float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    for K, legs in strikes.items():
        strike_gex = 0.0
        for typ in ("CE", "PE"):
            row = legs.get(typ)
            if not row:
                continue
            oi = _num(row, "oi")
            if oi <= 0:
                continue
            mult = _num(row, "lot_size") or 1.0
            prem = _num(row, "close") or _num(row, "settle")
            iv = implied_vol(prem, spot, K, T, typ == "CE")
            if not iv:
                continue
            g = bs_gamma(spot, K, T, iv)
            # ₹ dollar-gamma per 1% move: gamma * OI * mult * spot^2 * 0.01
            dollar_gamma = g * oi * mult * spot * spot * 0.01
            if typ == "CE":
                strike_gex += dollar_gamma       # dealers short calls -> +convention
                call_oi[K] = oi
            else:
                strike_gex -= dollar_gamma       # dealers long puts  -> -convention
                put_oi[K] = oi
        if strike_gex:
            by_strike[K] = strike_gex / GEX_SCALE
            net += strike_gex
            cum.append((K, strike_gex))

    net_scaled = net / GEX_SCALE
    call_wall = max(call_oi, key=call_oi.get) if call_oi else None
    put_wall = max(put_oi, key=put_oi.get) if put_oi else None

    # zero-gamma / flip strike: where the running cumulative GEX (low->high strike)
    # crosses zero — the level below which the market flips to negative gamma.
    zero_gamma = None
    run = 0.0
    prev_k = None
    for K, g in sorted(cum):
        new = run + g
        if prev_k is not None and (run <= 0 < new or run >= 0 > new):
            zero_gamma = K
        run, prev_k = new, K

    state = _state_label(net_scaled) if by_strike else "UNKNOWN"
    reasons = [f"net GEX {net_scaled:.2f}₹cr/1% -> {state}"]
    if state == "POSITIVE":
        reasons.append("dealers long gamma: vol-dampening, range/mean-revert (seller regime)")
    elif state == "NEGATIVE":
        reasons.append("dealers short gamma: vol-amplifying, trends persist (trend regime)")
    return DealerState(day=str(day)[:10], underlying=underlying, expiry=expiry, spot=spot,
                       net_gex=net_scaled, gex_state=state, call_wall=call_wall,
                       put_wall=put_wall, zero_gamma=zero_gamma, by_strike=by_strike,
                       reasons=reasons)


def from_store(store: Any, underlying: str, day: str,
               expiry: Optional[str] = None) -> DealerState:
    """Adapter: build the DealerState from a BhavcopyStore for one day. Picks the
    nearest (front) expiry unless one is given. Historically reconstructable."""
    chain = store.option_chain(underlying, day)
    if not chain:
        return DealerState(day=str(day)[:10], underlying=underlying, expiry=None, spot=0.0,
                           net_gex=0.0, gex_state="UNKNOWN", reasons=["no chain for day"])
    if expiry is None or expiry not in chain:
        expiry = sorted(chain.keys())[0]
    legs = chain[expiry]
    # spot: any row's underlying_price
    spot = 0.0
    for strike_legs in legs.values():
        for row in strike_legs.values():
            try:
                spot = float(row.get("underlying_price") or 0)
            except (TypeError, ValueError):
                spot = 0.0
            if spot > 0:
                break
        if spot > 0:
            break
    return dealer_state_from_chain(legs, spot, day, expiry, underlying=underlying)
