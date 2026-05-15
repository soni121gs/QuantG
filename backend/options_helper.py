"""Index-options helper for QuantG.

Resolves the current ATM/OTM option contract for a given underlying (NIFTY,
BANKNIFTY, SENSEX) using Zerodha's instruments dump. Caches the dump for 6h
because option chains change weekly with new expiries.

Lot sizes effective Jan 2026 (SEBI):
- NIFTY:    65
- BANKNIFTY: 30
- SENSEX:   20

Strike intervals on NSE/BSE:
- NIFTY:    50
- BANKNIFTY: 100
- SENSEX:   100
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("quantg.options")

# Hardcoded fallbacks. Kite's instruments dump is the source of truth at runtime.
LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20}
STRIKE_INTERVALS = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}
# Exchange where the OPTION trades (not the underlying)
OPT_EXCHANGE = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "SENSEX": "BFO"}
# Kite tradingsymbol for the SPOT index (used to fetch LTP)
INDEX_SPOT_SYMBOL = {
    "NIFTY": ("NSE", "NIFTY 50"),
    "BANKNIFTY": ("NSE", "NIFTY BANK"),
    "SENSEX": ("BSE", "SENSEX"),
}
# Indices we support
SUPPORTED = list(LOT_SIZES.keys())

# Cache: {exchange: {"instruments": [...], "cached_at": datetime}}
_OPT_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL_SEC = 6 * 3600  # 6 hours


def _load_instruments(kite, exchange: str) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    cached = _OPT_CACHE.get(exchange)
    if cached and (now - cached["cached_at"]).total_seconds() < _CACHE_TTL_SEC:
        return cached["instruments"]
    try:
        instruments = kite.instruments(exchange)
        _OPT_CACHE[exchange] = {"instruments": instruments, "cached_at": now}
        logger.info(f"options cache refreshed: {exchange} ({len(instruments)} instruments)")
        return instruments
    except Exception as e:
        logger.warning(f"instruments load failed for {exchange}: {e}")
        return cached["instruments"] if cached else []


def get_spot_ltp(kite, underlying: str) -> Optional[float]:
    """Get the current LTP of the index spot (NIFTY 50 / NIFTY BANK / SENSEX)."""
    if underlying not in INDEX_SPOT_SYMBOL:
        return None
    exch, sym = INDEX_SPOT_SYMBOL[underlying]
    try:
        key = f"{exch}:{sym}"
        ltp_resp = kite.ltp([key])
        if ltp_resp and key in ltp_resp:
            return float(ltp_resp[key]["last_price"])
    except Exception as e:
        logger.warning(f"spot ltp failed for {underlying}: {e}")
    return None


def round_to_strike(spot: float, interval: int) -> int:
    """Round spot to the nearest strike interval. e.g., NIFTY @ 24837 → 24850."""
    return int(round(spot / interval) * interval)


def resolve_atm_option(
    kite,
    underlying: str,
    option_type: str,   # "CE" or "PE"
    strike_offset_points: int = 0,
    expiry_offset_weeks: int = 0,
) -> Optional[Dict[str, Any]]:
    """Find the option contract Kite-side and return everything needed to place an order.

    strike_offset_points: 0 = ATM. +N for OTM CE / OTM PE depending on type.
        For a CE: ATM_strike + offset = higher strike = more OTM.
        For a PE: ATM_strike - offset = lower strike = more OTM.
    expiry_offset_weeks: 0 = current weekly, 1 = next week, ...

    Returns: {tradingsymbol, exchange, instrument_token, lot_size, strike, expiry}
             or None if not resolvable.
    """
    underlying = underlying.upper()
    option_type = option_type.upper()
    if underlying not in SUPPORTED:
        return None
    if option_type not in ("CE", "PE"):
        return None

    spot = get_spot_ltp(kite, underlying)
    if spot is None:
        logger.warning(f"cannot resolve ATM — spot LTP unavailable for {underlying}")
        return None

    interval = STRIKE_INTERVALS[underlying]
    atm = round_to_strike(spot, interval)
    # Apply directional offset: OTM strikes are AWAY from spot in the direction of price increase needed
    if option_type == "CE":
        target_strike = atm + strike_offset_points
    else:  # PE
        target_strike = atm - strike_offset_points

    # Snap to actual strike grid
    target_strike = round_to_strike(target_strike, interval)

    # Pull all option instruments for this underlying from the right exchange
    exchange = OPT_EXCHANGE[underlying]
    all_instruments = _load_instruments(kite, exchange)

    # Filter for our underlying + option_type. The 'name' field in Kite is the underlying name
    # e.g., name="NIFTY", instrument_type="CE", strike=24850, expiry=date(2026,2,26)
    candidates = [
        i for i in all_instruments
        if (i.get("name", "").upper() == underlying
            and i.get("instrument_type") == option_type
            and i.get("segment", "").endswith("-OPT"))
    ]
    if not candidates:
        logger.warning(f"no {option_type} option instruments found for {underlying} on {exchange}")
        return None

    # Sort unique expiries — pick the (expiry_offset_weeks)th nearest future expiry
    today = datetime.now(timezone.utc).date()
    future_expiries = sorted({c["expiry"] for c in candidates if c.get("expiry") and c["expiry"] >= today})
    if not future_expiries:
        logger.warning(f"no future expiries for {underlying} {option_type}")
        return None

    target_idx = min(expiry_offset_weeks, len(future_expiries) - 1)
    target_expiry = future_expiries[target_idx]

    # Pick the contract with matching strike & expiry
    for c in candidates:
        if c.get("expiry") == target_expiry and int(c.get("strike", 0)) == target_strike:
            return {
                "tradingsymbol": c["tradingsymbol"],
                "exchange": exchange,
                "instrument_token": int(c["instrument_token"]),
                "lot_size": int(c.get("lot_size") or LOT_SIZES.get(underlying, 1)),
                "strike": target_strike,
                "expiry": target_expiry.isoformat(),
                "underlying": underlying,
                "option_type": option_type,
                "spot": spot,
                "atm_strike": atm,
            }
    # If exact strike isn't listed (rare — strike chain usually covers ±2000 points),
    # fall back to the closest available strike for this expiry
    same_expiry = [c for c in candidates if c.get("expiry") == target_expiry]
    if same_expiry:
        nearest = min(same_expiry, key=lambda c: abs(int(c.get("strike", 0)) - target_strike))
        return {
            "tradingsymbol": nearest["tradingsymbol"],
            "exchange": exchange,
            "instrument_token": int(nearest["instrument_token"]),
            "lot_size": int(nearest.get("lot_size") or LOT_SIZES.get(underlying, 1)),
            "strike": int(nearest.get("strike", target_strike)),
            "expiry": target_expiry.isoformat(),
            "underlying": underlying,
            "option_type": option_type,
            "spot": spot,
            "atm_strike": atm,
            "note": f"exact strike {target_strike} not listed — using nearest {nearest.get('strike')}",
        }
    return None


def resolve_for_signal(
    kite,
    underlying: str,
    signal_action: str,    # "BUY" or "SELL" from strategy
    strike_mode: str,      # "ATM_BUY" | "OTM_BUY" | "ATM_SELL"
    otm_points: int = 0,
    expiry_offset_weeks: int = 0,
) -> Optional[Dict[str, Any]]:
    """Translate a strategy signal into a concrete option order.

    Modes:
      ATM_BUY  → BUY signal=buy CE at ATM; SELL signal=buy PE at ATM (directional bet)
      OTM_BUY  → BUY signal=buy CE at ATM+otm; SELL signal=buy PE at ATM-otm
      ATM_SELL → BUY signal=write PE at ATM (bullish); SELL signal=write CE at ATM (bearish)

    Returns the option contract + transaction_type (BUY or SELL) to place at the broker.
    """
    signal_action = signal_action.upper()
    strike_mode = strike_mode.upper()
    if signal_action not in ("BUY", "SELL"):
        return None
    if strike_mode not in ("ATM_BUY", "OTM_BUY", "ATM_SELL"):
        return None

    if strike_mode == "ATM_BUY":
        opt_type = "CE" if signal_action == "BUY" else "PE"
        contract = resolve_atm_option(kite, underlying, opt_type, 0, expiry_offset_weeks)
        if contract:
            contract["transaction_type"] = "BUY"  # buying premium
        return contract
    if strike_mode == "OTM_BUY":
        opt_type = "CE" if signal_action == "BUY" else "PE"
        contract = resolve_atm_option(kite, underlying, opt_type, otm_points, expiry_offset_weeks)
        if contract:
            contract["transaction_type"] = "BUY"
        return contract
    # ATM_SELL (option writing) — bullish view → short PE, bearish → short CE
    opt_type = "PE" if signal_action == "BUY" else "CE"
    contract = resolve_atm_option(kite, underlying, opt_type, 0, expiry_offset_weeks)
    if contract:
        contract["transaction_type"] = "SELL"  # writing premium
    return contract
