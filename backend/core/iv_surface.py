"""ERP P2-1: implied-volatility surface from stored bhavcopy option chains.

This module is deliberately pure and file-store based: no broker calls, no live
trading state. It converts one stored EOD option chain into strike IVs,
richness z-scores versus trailing stored history, and simple skew/term summaries.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import mean, pstdev
from typing import Any, Dict, List, Optional

from core.bhavcopy_store import BhavcopyStore

RISK_FREE_RATE = 0.06
MIN_T = 1.0 / 365.0


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot: float, strike: float, t: float, vol: float, option_type: str,
              rate: float = RISK_FREE_RATE) -> float:
    if spot <= 0 or strike <= 0 or t <= 0 or vol <= 0:
        return max(0.0, spot - strike) if option_type == "CE" else max(0.0, strike - spot)
    vt = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / vt
    d2 = d1 - vt
    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * t) * _norm_cdf(d2)
    return strike * math.exp(-rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t: float, option_type: str) -> Optional[float]:
    """Bisection IV solver. Returns None for impossible/empty option marks."""
    price = float(price or 0.0)
    spot = float(spot or 0.0)
    strike = float(strike or 0.0)
    t = max(float(t or 0.0), MIN_T)
    opt = str(option_type or "").upper()
    if price <= 0 or spot <= 0 or strike <= 0 or opt not in {"CE", "PE"}:
        return None
    intrinsic = max(0.0, spot - strike) if opt == "CE" else max(0.0, strike - spot)
    if price < intrinsic * 0.98:
        return None
    lo, hi = 0.01, 5.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        model = _bs_price(spot, strike, t, mid, opt)
        if model < price:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2.0, 4)


def _years_to_expiry(day: str, expiry: str) -> float:
    try:
        d0 = date.fromisoformat(str(day)[:10])
        d1 = date.fromisoformat(str(expiry)[:10])
    except ValueError:
        return MIN_T
    return max((d1 - d0).days / 365.0, MIN_T)


def _spot_from_chain(chain: Dict[str, Dict[float, Dict[str, Any]]]) -> float:
    spots = []
    for strikes in chain.values():
        for legs in strikes.values():
            for row in legs.values():
                spot = _f(row.get("underlying_price"))
                if spot > 0:
                    spots.append(spot)
    return mean(spots) if spots else 0.0


@dataclass
class SurfacePoint:
    expiry: str
    strike: float
    option_type: str
    iv: float
    price: float
    moneyness: float
    oi: float
    volume: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "iv": self.iv,
            "price": self.price,
            "moneyness": round(self.moneyness, 4),
            "oi": self.oi,
            "volume": self.volume,
        }


def surface_for_day(store: BhavcopyStore, underlying: str, day: str) -> Dict[str, Any]:
    chain = store.option_chain(underlying, day)
    spot = _spot_from_chain(chain)
    points: List[SurfacePoint] = []
    for expiry, strikes in chain.items():
        t = _years_to_expiry(day, expiry)
        for strike, legs in strikes.items():
            for opt, row in legs.items():
                price = _f(row.get("close")) or _f(row.get("settle"))
                iv = implied_vol(price, spot, strike, t, opt)
                if iv is None:
                    continue
                points.append(SurfacePoint(
                    expiry=expiry,
                    strike=float(strike),
                    option_type=opt,
                    iv=iv,
                    price=price,
                    moneyness=float(strike) / spot if spot else 0.0,
                    oi=_f(row.get("oi")),
                    volume=_f(row.get("volume")),
                ))
    if not points:
        return {"available": False, "underlying": underlying.upper(), "date": day,
                "reason": "no solvable option IV points"}
    expiries = sorted({p.expiry for p in points})
    near = expiries[0]
    near_points = [p for p in points if p.expiry == near]
    atm = min(near_points, key=lambda p: abs(p.moneyness - 1.0))
    puts = [p.iv for p in near_points if p.option_type == "PE" and p.moneyness < 0.98]
    calls = [p.iv for p in near_points if p.option_type == "CE" and p.moneyness > 1.02]
    term = []
    for exp in expiries:
        xs = [p.iv for p in points if p.expiry == exp and 0.9 <= p.moneyness <= 1.1]
        if xs:
            term.append({"expiry": exp, "atm_band_iv": round(mean(xs), 4), "points": len(xs)})
    return {
        "available": True,
        "underlying": underlying.upper(),
        "date": day,
        "spot": round(spot, 2),
        "atm_iv": atm.iv,
        "points": [p.as_dict() for p in points],
        "summary": {
            "point_count": len(points),
            "expiries": expiries,
            "near_expiry": near,
            "put_call_skew": round((mean(puts) if puts else 0.0) - (mean(calls) if calls else 0.0), 4),
            "term_structure": term,
        },
    }


def richness_zscore(store: BhavcopyStore, underlying: str, day: str, *, lookback_days: int = 60) -> Dict[str, Any]:
    days = [d for d in store.trading_days(end=day) if d < day][-lookback_days:]
    current = surface_for_day(store, underlying, day)
    if not current.get("available"):
        return {**current, "richness": {"available": False, "reason": current.get("reason")}}
    hist = []
    for d in days:
        surf = surface_for_day(store, underlying, d)
        if surf.get("available"):
            hist.append(float(surf.get("atm_iv") or 0.0))
    if len(hist) < 10:
        return {**current, "richness": {"available": False, "sample_n": len(hist),
                                        "reason": "need at least 10 historical surfaces"}}
    mu = mean(hist)
    sd = pstdev(hist)
    z = 0.0 if sd <= 0 else (float(current["atm_iv"]) - mu) / sd
    return {**current, "richness": {
        "available": True,
        "sample_n": len(hist),
        "mean_atm_iv": round(mu, 4),
        "stdev_atm_iv": round(sd, 4),
        "zscore": round(z, 3),
        "state": "rich" if z >= 1.0 else "cheap" if z <= -1.0 else "neutral",
    }}
