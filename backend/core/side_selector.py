"""RES-5 — dynamic side rotation (CE vs PE) from the market context.

Turns the market_context.seller_decision (RES-2) into a concrete spread-entry
instruction the spread builder understands. This is the "trade calls when calls
are hot, flip when it tops" logic — but driven by regime + chain skew, not
intuition. PURE, so the scalper and the backtester rotate identically.

Convention (matches the live book):
  PE credit spread  = bull-put   (direction "bullish") — sell when tape is basing/up
  CE credit spread  = bear-call  (direction "bearish") — sell when tape is topping/down
"""
from __future__ import annotations

from typing import Any, Dict, Optional

_SIDE_TO_DIRECTION = {"PE": "bullish", "CE": "bearish"}


def side_to_direction(side: Optional[str]) -> Optional[str]:
    """PE → bullish (bull-put), CE → bearish (bear-call)."""
    if side is None:
        return None
    return _SIDE_TO_DIRECTION.get(str(side).upper())


def select_sell_spread(context: Dict[str, Any]) -> Dict[str, Any]:
    """Given a market_context (from market_context.build_context), return the
    entry instruction {ok, structure, side, direction, reason} or {ok: False}
    when the gates say don't sell. The scalper passes this straight to the
    spread builder."""
    decision = (context or {}).get("seller_decision") or {}
    if not decision.get("allow_sell"):
        reasons = decision.get("reasons") or ["seller gates not satisfied"]
        return {"ok": False, "structure": None, "side": None, "direction": None,
                "reason": "; ".join(reasons)}
    side = decision.get("side")
    direction = side_to_direction(side)
    if direction is None:
        return {"ok": False, "structure": None, "side": None, "direction": None,
                "reason": f"no tradeable side (side={side})"}
    return {"ok": True, "structure": "credit_spread", "side": side,
            "direction": direction,
            "reason": f"sell {side} {direction} spread ({'; '.join(decision.get('reasons') or [])})"}
