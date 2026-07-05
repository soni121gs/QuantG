"""IMD-06: historical no-lookahead option selection replay.

Reproduces the live app's strike/contract choice at a historical minute WITHOUT
peeking at future candles. The caller passes a chain snapshot built by
``OptionsMinuteStore.get_chain_at_time`` (which only contains bars at/<= the
timestamp) plus the underlying's price at that minute; this module is a pure
function of those inputs, so no future bar can influence the pick.

Supports the structures QG-O5..QG-O10 use: ``single_leg`` (buy one option) and
``debit_spread`` (buy near leg, sell the wing ``spread_width`` strikes away).
Lot size comes from ``core.market_domains``. A missing leg returns a TYPED reason,
never a guessed contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.market_domains import resolve_domain_by_underlying

# typed reasons
OK = "OK"
NO_CHAIN = "NO_CHAIN"
NO_EXPIRY = "NO_EXPIRY"
BAD_DIRECTION = "BAD_DIRECTION"
NO_STRIKE = "NO_STRIKE"
MISSING_LEG = "MISSING_LEG"


@dataclass(frozen=True)
class Leg:
    strike: float
    option_type: str
    side: str            # BUY / SELL
    price: float
    instrument_key: str
    expired_instrument_key: str


@dataclass(frozen=True)
class Selection:
    ok: bool
    reason: str
    structure: str = ""
    expiry: str = ""
    lot_size: int = 0
    legs: List[Leg] = field(default_factory=list)
    detail: str = ""


def _nearest_expiry(chain: Dict[str, Any]) -> Optional[str]:
    return sorted(chain.keys())[0] if chain else None


def _nearest_strike(strikes: List[float], target: float) -> Optional[float]:
    return min(strikes, key=lambda s: abs(s - target)) if strikes else None


def _leg(node: Dict[str, Dict[str, Any]], strike: float, opt: str, side: str) -> Optional[Leg]:
    row = node.get(opt)
    if not row:
        return None
    return Leg(
        strike=strike, option_type=opt, side=side, price=float(row.get("close") or 0.0),
        instrument_key=str(row.get("instrument_key") or ""),
        expired_instrument_key=str(row.get("expired_instrument_key") or ""),
    )


def select_contract(
    *,
    underlying: str,
    spot: float,
    chain: Dict[str, Dict[float, Dict[str, Dict[str, Any]]]],
    direction: str,
    structure: str = "single_leg",
    otm_offset_strikes: int = 0,
    spread_width: int = 2,
    expiry: Optional[str] = None,
) -> Selection:
    """Pick the contract(s) a strategy would trade at this minute. Pure, no lookahead."""
    opt = str(direction).upper()
    if opt not in ("CE", "PE"):
        return Selection(False, BAD_DIRECTION, detail=direction)
    if not chain:
        return Selection(False, NO_CHAIN)

    expiry = expiry or _nearest_expiry(chain)
    strikes_map = chain.get(expiry) if expiry else None
    if not strikes_map:
        return Selection(False, NO_EXPIRY, detail=str(expiry))

    step = resolve_domain_by_underlying(underlying).get_strike_interval(underlying)
    lot = resolve_domain_by_underlying(underlying).get_lot_size(underlying)
    strikes = sorted(strikes_map.keys())

    # near strike: ATM (nearest to spot), shifted OTM by offset strikes in the
    # out-of-the-money direction (CE up, PE down).
    atm = _nearest_strike(strikes, spot)
    if atm is None:
        return Selection(False, NO_STRIKE)
    otm_sign = 1 if opt == "CE" else -1
    near_target = atm + otm_sign * otm_offset_strikes * step
    near_strike = _nearest_strike(strikes, near_target)

    near_leg = _leg(strikes_map[near_strike], near_strike, opt, "BUY")
    if near_leg is None:
        return Selection(False, MISSING_LEG, detail=f"near {near_strike} {opt}")

    if structure == "single_leg":
        return Selection(True, OK, "single_leg", expiry, lot, [near_leg])

    if structure == "debit_spread":
        # sell the wing exactly spread_width strikes further OTM (CE: higher, PE:
        # lower). The width defines the risk — never snap to a different strike.
        wing_strike = near_strike + otm_sign * spread_width * step
        if wing_strike not in strikes_map:
            return Selection(False, MISSING_LEG, detail=f"wing {wing_strike} {opt}")
        wing_leg = _leg(strikes_map[wing_strike], wing_strike, opt, "SELL")
        if wing_leg is None:
            return Selection(False, MISSING_LEG, detail=f"wing {wing_strike} {opt}")
        return Selection(True, OK, "debit_spread", expiry, lot, [near_leg, wing_leg])

    return Selection(False, BAD_DIRECTION, detail=f"structure={structure}")
