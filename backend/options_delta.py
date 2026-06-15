"""Phase 2 #2 — delta-based option strike selection.

Picks the option strike whose |delta| is closest to a per-risk-style target,
from the option chain that `_resolve_option` already fetches for greeks. Pure
and tolerant of missing fields so the caller can fall back to the resolver's
default ATM/OTM strike whenever delta data is absent.

Lower target delta → more OTM (cheaper premium, higher leverage, lower win-prob);
higher → toward ATM. ATM ≈ 0.50 (observed: NIFTY/BANKNIFTY ATM δ≈0.48 on 06-15).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

OPTION_DELTA_SELECTION_ENABLED = (
    os.environ.get("OPTION_DELTA_SELECTION_ENABLED", "true").strip().lower() == "true"
)

# Target |delta| per risk_style.
DELTA_TARGETS: Dict[str, float] = {
    "micro_scalp": 0.35,
    "momentum": 0.45,
    "breakout": 0.50,
    "volatile_breakout": 0.50,
    "pullback": 0.40,
    "balanced": 0.45,
}
DELTA_TARGET_DEFAULT = float(os.environ.get("OPTION_DELTA_TARGET_DEFAULT", "0.45"))
# Don't select an illiquid near-zero-premium strike just because its delta matches.
DELTA_MIN_LTP = float(os.environ.get("OPTION_DELTA_MIN_LTP", "0.5"))


def target_delta_for_style(risk_style: Optional[str]) -> float:
    return DELTA_TARGETS.get(str(risk_style or "").strip().lower(), DELTA_TARGET_DEFAULT)


def _to_float(v: Any) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pick_delta_strike(
    chain_nodes: Optional[List[Dict[str, Any]]],
    option_side: str,
    target_delta: float,
    *,
    min_ltp: float = DELTA_MIN_LTP,
) -> Optional[Dict[str, Any]]:
    """Return the chain node whose |delta| is closest to ``target_delta`` for the
    given option_side ("CE"/"PE"). Returns a dict with keys
    {strike, expiry, delta, opt_node} or None if no usable strike exists.

    A node is usable only if it has a delta, a positive LTP ≥ min_ltp, and an
    instrument_key — so the caller can safely re-point the order to it.
    """
    side_key = "call_options" if str(option_side).upper() == "CE" else "put_options"
    best: Optional[Dict[str, Any]] = None
    best_dist: Optional[float] = None
    for node in chain_nodes or []:
        opt = node.get(side_key) or {}
        if not opt.get("instrument_key"):
            continue
        delta = _to_float((opt.get("option_greeks") or {}).get("delta"))
        if delta is None:
            continue
        md = opt.get("market_data") or {}
        ltp = _to_float(md.get("ltp")) or _to_float(md.get("last_price")) or 0.0
        if ltp < min_ltp:
            continue
        dist = abs(abs(delta) - float(target_delta))
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = {
                "strike": _to_float(node.get("strike_price")),
                "expiry": node.get("expiry"),
                "delta": delta,
                "ltp": ltp,
                "opt_node": opt,
            }
    return best
