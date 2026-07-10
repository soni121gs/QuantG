"""RAE-4 (2026-07-10): the Router / Capital Allocator — the piece that replaces
"run every strategy every day" with "run the day's specialist(s)" (CLAUDE.md §18.2 #4).

Given the current regime + confidence (RAE-1) and the specialist role a strategy
plays, it returns a continuous SIZE MULTIPLIER (0 = stand down) plus which
specialist(s) own the regime. Downstream, EdgeMath (§16) turns that multiplier into
lots — the router decides WHETHER and HOW MUCH by regime; EdgeMath decides the base
size from rolling edge. Stand-down is expressed as `size_mult == 0`, not a veto
exception — the EM "continuous size, no hard block" philosophy.

The single most important live effect: a `range_seller` gets `size_mult 0` when the
regime is not RANGE/INSIDE — i.e. it STANDS DOWN instead of selling premium into a
trend (the 2026-07-10 loss) or a chop day (the −₹1,162/day tail). That is RAE-3a +
RAE-3d enforced in one place.

Pure, no I/O — identical in the OOS validator (fed a RAE-1 snapshot) and the live
hook (fed the coarse live regime). Live enforcement is env-gated OFF by default
(`RAE_ROUTER_ENABLED`); until the founder flips it the router only annotates.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core import regime_taxonomy as tax


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    """Live enforcement gate. OFF by default — observe-only until the founder flips it."""
    return os.environ.get("RAE_ROUTER_ENABLED", "false").lower() == "true"


# Per-regime base size multiplier (before EdgeMath). CHOP/EVENT = 0 (stand down).
REGIME_SIZE = {
    tax.TREND_UP: _f("RAE_SIZE_TREND", 1.0),
    tax.TREND_DOWN: _f("RAE_SIZE_TREND", 1.0),
    tax.RANGE: _f("RAE_SIZE_RANGE", 1.0),
    tax.INSIDE_QUIET: _f("RAE_SIZE_INSIDE", 0.8),
    tax.HIGH_VOL_CHOP: 0.0,
    tax.EVENT: 0.0,
}
TREND_MIN_CONF = _f("RAE_ROUTER_TREND_CONF", 0.90)   # trend needs high confidence (precision)
# sellers co-own the quiet regimes with the mean-revert specialist
SELLER_OK_REGIMES = {tax.RANGE, tax.INSIDE_QUIET}
# a single delta-1 trend specialist owns BOTH trend directions (its code picks CE vs
# PE by the day's direction); accept the generic role name for either trend owner.
TREND_OK_REGIMES = {tax.TREND_UP, tax.TREND_DOWN}
TREND_ROLES = {"trend_delta1", "trend_delta1_long", "trend_delta1_short"}


@dataclass
class RoutingDecision:
    regime: str
    confidence: float
    stand_down: bool
    size_mult: float
    active_specialists: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"regime": self.regime, "confidence": round(self.confidence, 3),
                "stand_down": self.stand_down, "size_mult": round(self.size_mult, 3),
                "active_specialists": list(self.active_specialists), "reasons": list(self.reasons)}


def _stand_down(regime: str, conf: float, reason: str) -> RoutingDecision:
    return RoutingDecision(regime, conf, True, 0.0, [], [reason])


def route(
    regime_label: str,
    confidence: float = 0.5,
    *,
    specialist: Optional[str] = None,
) -> RoutingDecision:
    """Decide whether/how much to trade in `regime_label` for a strategy playing the
    `specialist` role (from `regime_taxonomy.REGIME_OWNER` values, e.g. 'range_seller').
    When `specialist` is None the decision is regime-generic (owner activated)."""
    regime = str(regime_label or "").upper()
    if regime not in tax.REGIME_LABELS:
        # coarse/unknown live regime → treat as RANGE (sellers' home), size 1
        regime = tax.RANGE
    owner = tax.REGIME_OWNER.get(regime, "range_seller")

    # 1) hard stand-down regimes (nothing wins / fat tails)
    if regime in (tax.HIGH_VOL_CHOP, tax.EVENT):
        return _stand_down(regime, confidence, f"{regime}: stand down (no edge / fat-tail)")

    # 2) specialist ownership — a strategy only trades the regime(s) it owns
    if specialist is not None:
        owns = (
            (specialist == owner)
            or (specialist == "range_seller" and regime in SELLER_OK_REGIMES)
            or (specialist in TREND_ROLES and regime in TREND_OK_REGIMES)
        )
        if not owns:
            return _stand_down(regime, confidence,
                               f"{specialist} does not own {regime} (owner={owner}) — stand down")

    # 3) trend needs high confidence (the 498-day study: bare trend calls are 16% precise)
    if regime in (tax.TREND_UP, tax.TREND_DOWN) and confidence < TREND_MIN_CONF:
        return _stand_down(regime, confidence,
                           f"{regime} confidence {confidence:.2f} < {TREND_MIN_CONF} — likely fakeout, stand down")

    base = REGIME_SIZE.get(regime, 1.0)
    # trend size scales with how far past the confidence gate we are (precision → size)
    if regime in (tax.TREND_UP, tax.TREND_DOWN) and TREND_MIN_CONF < 1.0:
        scale = (confidence - TREND_MIN_CONF) / (1.0 - TREND_MIN_CONF)
        base *= max(0.0, min(1.0, 0.5 + 0.5 * scale))   # 0.5..1.0 across the confident band
    active = [specialist] if specialist else [owner]
    return RoutingDecision(regime, confidence, False, base, active,
                           [f"{regime}: activate {active[0]} at size×{base:.2f}"])
