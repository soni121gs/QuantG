"""Phase 2 #5 — defined-risk credit spread builder (pure).

Builds a two-leg vertical credit spread from an option chain:
  - bullish signal  → bull put spread  (SELL higher-strike PE, BUY lower-strike PE)
  - bearish signal  → bear call spread (SELL lower-strike CE, BUY higher-strike CE)

The short leg is chosen near a target |delta|; the long leg is `width_points`
away to cap the loss. Returns net credit, max loss, and net greeks so the risk
manager can size by defined risk (max loss), not premium. No DB, no I/O — the
caller passes in the already-fetched chain nodes.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from options_delta import pick_delta_strike, _to_float  # noqa: E402

CREDIT_SPREADS_ENABLED = os.environ.get("CREDIT_SPREADS_ENABLED", "false").strip().lower() == "true"
CREDIT_SPREAD_SHORT_DELTA = float(os.environ.get("CREDIT_SPREAD_SHORT_DELTA", "0.30"))
# Default distance between short and long strikes, in number of strike intervals.
CREDIT_SPREAD_WIDTH_STRIKES = int(os.environ.get("CREDIT_SPREAD_WIDTH_STRIKES", "2"))


def _node_strike(node: Dict[str, Any]) -> Optional[float]:
    return _to_float(node.get("strike_price"))


def _leg_from_node(node: Dict[str, Any], option_type: str, side: str) -> Optional[Dict[str, Any]]:
    side_key = "call_options" if option_type == "CE" else "put_options"
    opt = node.get(side_key) or {}
    key = opt.get("instrument_key")
    if not key:
        return None
    md = opt.get("market_data") or {}
    greeks = opt.get("option_greeks") or {}
    premium = _to_float(md.get("ltp")) or _to_float(md.get("last_price"))
    if premium is None or premium <= 0:
        return None
    return {
        "role": "short" if side == "SELL" else "long",
        "side": side,
        "option_type": option_type,
        "strike": _node_strike(node),
        "instrument_key": key,
        "tradingsymbol": opt.get("trading_symbol"),
        "expiry": node.get("expiry"),
        "premium": premium,
        "delta": _to_float(greeks.get("delta")),
        "iv": _to_float(greeks.get("iv")),
        "theta": _to_float(greeks.get("theta")),
        "oi": _to_float(md.get("oi")),
    }


def _find_node_by_strike(nodes: List[Dict[str, Any]], strike: float, option_type: str) -> Optional[Dict[str, Any]]:
    """Exact strike match, else the nearest strike that has a usable leg."""
    exact = None
    nearest = None
    nearest_dist = None
    side_key = "call_options" if option_type == "CE" else "put_options"
    for node in nodes or []:
        ns = _node_strike(node)
        if ns is None:
            continue
        opt = node.get(side_key) or {}
        if not opt.get("instrument_key"):
            continue
        if int(ns) == int(strike):
            exact = node
            break
        dist = abs(ns - strike)
        if nearest_dist is None or dist < nearest_dist:
            nearest_dist = dist
            nearest = node
    return exact or nearest


def build_credit_spread(
    *,
    chain_nodes: List[Dict[str, Any]],
    direction: str,
    width_points: float,
    short_delta: float = CREDIT_SPREAD_SHORT_DELTA,
) -> Dict[str, Any]:
    """Build a vertical credit spread.

    direction: "bullish" → bull put spread; "bearish" → bear call spread.
    width_points: absolute strike distance between short and long legs.

    Returns {ok, reason, structure, direction, option_type, short_leg, long_leg,
    net_credit, max_loss, max_profit, width_points, net_delta, net_theta}.
    All money figures are PER UNIT (premium points); caller multiplies by
    lot_size * lots.
    """
    direction = str(direction or "").lower()
    if direction not in ("bullish", "bearish"):
        return {"ok": False, "reason": "direction must be bullish or bearish"}
    option_type = "PE" if direction == "bullish" else "CE"

    short_pick = pick_delta_strike(chain_nodes, option_type, short_delta)
    if not short_pick or short_pick.get("strike") is None:
        return {"ok": False, "reason": "no short-leg strike near target delta"}
    short_strike = float(short_pick["strike"])

    # Bull put: long strike BELOW short. Bear call: long strike ABOVE short.
    long_strike = short_strike - width_points if direction == "bullish" else short_strike + width_points

    short_node = _find_node_by_strike(chain_nodes, short_strike, option_type)
    long_node = _find_node_by_strike(chain_nodes, long_strike, option_type)
    if not short_node or not long_node:
        return {"ok": False, "reason": "could not locate both spread legs in chain"}

    short_leg = _leg_from_node(short_node, option_type, "SELL")
    long_leg = _leg_from_node(long_node, option_type, "BUY")
    if not short_leg or not long_leg:
        return {"ok": False, "reason": "spread leg missing instrument_key or premium"}
    if short_leg["strike"] == long_leg["strike"]:
        return {"ok": False, "reason": "short and long legs resolved to the same strike"}

    net_credit = round(short_leg["premium"] - long_leg["premium"], 2)
    if net_credit <= 0:
        return {"ok": False, "reason": f"non-positive net credit ({net_credit})"}

    actual_width = abs(short_leg["strike"] - long_leg["strike"])
    max_loss = round(actual_width - net_credit, 2)
    if max_loss <= 0:
        return {"ok": False, "reason": f"non-positive max loss ({max_loss})"}

    sd = short_leg.get("delta") or 0.0
    ld = long_leg.get("delta") or 0.0
    st = short_leg.get("theta") or 0.0
    lt = long_leg.get("theta") or 0.0
    return {
        "ok": True,
        "reason": "ok",
        "structure": "credit_spread",
        "direction": direction,
        "option_type": option_type,
        "short_leg": short_leg,
        "long_leg": long_leg,
        "net_credit": net_credit,        # max profit per unit
        "max_profit": net_credit,
        "max_loss": max_loss,            # defined risk per unit
        "width_points": actual_width,
        # Net greeks: short leg is a SOLD option, so its greeks flip sign.
        "net_delta": round(-sd + ld, 4),
        "net_theta": round(-st + lt, 4),
    }


def build_credit_spread_by_offset(
    *,
    chain_nodes: List[Dict[str, Any]],
    direction: str,
    spot: float,
    offset_strikes: int,
    width_points: float,
) -> Dict[str, Any]:
    """Build a credit spread by offset from ATM instead of target delta.

    Used by QG-O5's intraday credit scalp where the OOS lead was defined as:
    bullish ORB -> sell PE 2 strikes OTM, buy 1 strike lower.
    """
    direction = str(direction or "").lower()
    if direction not in ("bullish", "bearish"):
        return {"ok": False, "reason": "direction must be bullish or bearish"}
    option_type = "PE" if direction == "bullish" else "CE"
    strikes = sorted(s for s in (_node_strike(n) for n in chain_nodes or []) if s is not None)
    if not strikes:
        return {"ok": False, "reason": "no strikes in chain"}

    atm = min(strikes, key=lambda s: abs(s - float(spot)))
    sign = -1 if direction == "bullish" else 1
    short_strike = atm + sign * max(0, int(offset_strikes)) * abs(float(width_points))
    long_strike = short_strike + sign * abs(float(width_points))

    short_node = _find_node_by_strike(chain_nodes, short_strike, option_type)
    long_node = _find_node_by_strike(chain_nodes, long_strike, option_type)
    if not short_node or not long_node:
        return {"ok": False, "reason": "could not locate both spread legs in chain"}

    short_leg = _leg_from_node(short_node, option_type, "SELL")
    long_leg = _leg_from_node(long_node, option_type, "BUY")
    if not short_leg or not long_leg:
        return {"ok": False, "reason": "spread leg missing instrument_key or premium"}
    if short_leg["strike"] == long_leg["strike"]:
        return {"ok": False, "reason": "short and long legs resolved to the same strike"}

    net_credit = round(short_leg["premium"] - long_leg["premium"], 2)
    if net_credit <= 0:
        return {"ok": False, "reason": f"non-positive net credit ({net_credit})"}

    actual_width = abs(short_leg["strike"] - long_leg["strike"])
    max_loss = round(actual_width - net_credit, 2)
    if max_loss <= 0:
        return {"ok": False, "reason": f"non-positive max loss ({max_loss})"}

    sd = short_leg.get("delta") or 0.0
    ld = long_leg.get("delta") or 0.0
    st = short_leg.get("theta") or 0.0
    lt = long_leg.get("theta") or 0.0
    return {
        "ok": True,
        "reason": "ok",
        "structure": "credit_spread",
        "direction": direction,
        "option_type": option_type,
        "short_leg": short_leg,
        "long_leg": long_leg,
        "net_credit": net_credit,
        "max_profit": net_credit,
        "max_loss": max_loss,
        "width_points": actual_width,
        "net_delta": round(-sd + ld, 4),
        "net_theta": round(-st + lt, 4),
        "selection_method": "offset",
        "offset_strikes": int(offset_strikes),
    }


DEBIT_SPREADS_ENABLED = os.environ.get("DEBIT_SPREADS_ENABLED", "true").strip().lower() == "true"
DEBIT_SPREAD_LONG_DELTA = float(os.environ.get("DEBIT_SPREAD_LONG_DELTA", "0.50"))


def build_debit_spread(
    *,
    chain_nodes: List[Dict[str, Any]],
    direction: str,
    width_points: float,
    long_delta: float = DEBIT_SPREAD_LONG_DELTA,
) -> Dict[str, Any]:
    """Build a vertical debit spread.

    direction: "bullish" → bull call spread (BUY near-ATM CE, SELL further-OTM CE);
               "bearish" → bear put spread (BUY near-ATM PE, SELL further-OTM PE).
    width_points: absolute strike distance between long and short legs.

    Returns {ok, reason, structure, direction, option_type, short_leg, long_leg,
    net_debit, max_loss, max_profit, width_points, net_delta, net_theta}.
    All money figures are PER UNIT (premium points); caller multiplies by
    lot_size * lots.
    """
    direction = str(direction or "").lower()
    if direction not in ("bullish", "bearish"):
        return {"ok": False, "reason": "direction must be bullish or bearish"}
    option_type = "CE" if direction == "bullish" else "PE"

    long_pick = pick_delta_strike(chain_nodes, option_type, long_delta)
    if not long_pick or long_pick.get("strike") is None:
        return {"ok": False, "reason": "no long-leg strike near target delta"}
    long_strike = float(long_pick["strike"])

    # Bull call: short strike ABOVE long. Bear put: short strike BELOW long.
    short_strike = long_strike + width_points if direction == "bullish" else long_strike - width_points

    long_node = _find_node_by_strike(chain_nodes, long_strike, option_type)
    short_node = _find_node_by_strike(chain_nodes, short_strike, option_type)
    if not short_node or not long_node:
        return {"ok": False, "reason": "could not locate both spread legs in chain"}

    long_leg = _leg_from_node(long_node, option_type, "BUY")
    short_leg = _leg_from_node(short_node, option_type, "SELL")
    if not short_leg or not long_leg:
        return {"ok": False, "reason": "spread leg missing instrument_key or premium"}
    if short_leg["strike"] == long_leg["strike"]:
        return {"ok": False, "reason": "short and long legs resolved to the same strike"}

    net_debit = round(long_leg["premium"] - short_leg["premium"], 2)
    if net_debit <= 0:
        return {"ok": False, "reason": f"non-positive net debit ({net_debit})"}

    actual_width = abs(short_leg["strike"] - long_leg["strike"])
    max_profit = round(actual_width - net_debit, 2)
    if max_profit <= 0:
        return {"ok": False, "reason": f"non-positive max profit ({max_profit})"}

    sd = short_leg.get("delta") or 0.0
    ld = long_leg.get("delta") or 0.0
    st = short_leg.get("theta") or 0.0
    lt = long_leg.get("theta") or 0.0
    return {
        "ok": True,
        "reason": "ok",
        "structure": "debit_spread",
        "direction": direction,
        "option_type": option_type,
        "short_leg": short_leg,
        "long_leg": long_leg,
        "net_debit": net_debit,          # net cost per unit
        "max_profit": max_profit,
        "max_loss": net_debit,           # defined risk per unit
        "width_points": actual_width,
        # Net greeks: short leg is sold (-), long leg is bought (+)
        "net_delta": round(ld - sd, 4),
        "net_theta": round(lt - st, 4),
    }


def lots_for_risk(max_loss_per_unit: float, lot_size: int, risk_budget: float) -> int:
    """Number of lots whose total defined risk stays within risk_budget."""
    per_lot = float(max_loss_per_unit) * int(lot_size)
    if per_lot <= 0 or risk_budget <= 0:
        return 0
    return max(0, int(risk_budget // per_lot))
