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


def score_size_neutral() -> bool:
    """2026-08-02 (M5): the score-IC screen (scripts/run_score_ic.py) found the RAE
    regime CONFIDENCE has a significantly NEGATIVE information coefficient vs realized
    P&L (IC −0.18, t −2.33) — higher confidence predicted WORSE outcomes — and the
    contract/EdgeMath scores had ~zero IC. So scaling position size by the confidence
    MAGNITUDE is sizing on (inverted) noise. When true (default), the router uses a
    FLAT per-regime base and does NOT scale by confidence; confidence is still used
    CATEGORICALLY (stand-down + the trend precision gate), which is a different, sound
    decision. Reversible: set SCORE_SIZE_NEUTRAL=false to restore magnitude scaling."""
    return os.environ.get("SCORE_SIZE_NEUTRAL", "true").lower() == "true"


def chop_standdown_enabled() -> bool:
    """HIGH_VOL_CHOP veto. ON by default (sellers stand down on chop — the RAE
    design). Founder-directed 2026-07-16: set RAE_CHOP_STANDDOWN=false to turn the
    chop veto OFF — a chop day is then treated as a wide RANGE and each specialist's
    OWN regime gate decides, so range sellers trade on chop instead of standing down.
    EVENT stays a hard stand-down regardless (macro fat-tail). Reversible via env."""
    return os.environ.get("RAE_CHOP_STANDDOWN", "true").lower() == "true"


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
# Below this, the fine intraday read is "we don't know yet" rather than a regime
# call, and we defer to the coarse (VWAP/whole-session) regime, which is mature
# from the open. See the 2026-07-22 note in `route()`.
FINE_MIN_CONF = _f("RAE_ROUTER_FINE_MIN_CONF", 0.50)
# 2026-08-02 (P5-R3): the seller-size scaling reference. It MUST be the ceiling a
# mature RANGE can actually reach, not FINE_MIN_CONF. `regime_classifier` returns
# RANGE confidence = RANGE_BASE_CONF (0.40) × maturity, so an established 200-bar
# range tops out at 0.40. Scaling that by FINE_MIN_CONF (0.50) meant a fully-mature
# range was permanently capped at 0.40/0.50 = 0.8 size — it deferred exactly like a
# 10-bar one, the gap R3 was filed for. Referencing the RANGE ceiling instead lets a
# mature range earn full size while an immature one is still throttled by maturity.
# Kept a hair below RANGE_BASE_CONF's 0.40 default so rounding never starves a mature
# range; env-overridable to track RAE_RANGE_BASE_CONF if that is retuned.
SELLER_SIZE_CONF_REF = _f("RAE_SELLER_SIZE_CONF_REF", 0.40)
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


def _stand_down(regime: str, conf: float, reason: str,
                notes: Optional[List[str]] = None) -> RoutingDecision:
    return RoutingDecision(regime, conf, True, 0.0, [], [*(notes or []), reason])


def _more_conservative(a: RoutingDecision, b: RoutingDecision) -> RoutingDecision:
    """Whichever decision risks less: a stand-down beats trading; else smaller size."""
    if a.stand_down != b.stand_down:
        return a if a.stand_down else b
    return a if a.size_mult <= b.size_mult else b


def route(
    regime_label: str,
    confidence: float = 0.5,
    *,
    specialist: Optional[str] = None,
    fallback_regime: Optional[str] = None,
) -> RoutingDecision:
    """Decide whether/how much to trade in `regime_label` for a strategy playing the
    `specialist` role (from `regime_taxonomy.REGIME_OWNER` values, e.g. 'range_seller').
    When `specialist` is None the decision is regime-generic (owner activated).

    `fallback_regime` is the COARSE regime (`market_regime_state.regime`, VWAP-based
    over the whole session) and acts as a CROSS-CHECK, never a replacement.

    Why a cross-check and not a substitution (2026-07-23, P5-R1): RANGE is the
    classifier's FALL-THROUGH label — "no decisive trend/chop/inside signature" — and
    it is also the premium sellers' home regime, so "we don't know yet" used to read
    as a full-size green light. That cost ₹2.2k on 2026-07-22, when the fine read said
    RANGE/0.40 all morning while the mature coarse read said TREND_DOWN.

    The first fix rewrote `regime` to the coarse label at the top of this function and
    broke three things, because it ran ABOVE every protective guard below:
      * the long-vol guard never saw HIGH_VOL_CHOP, so the long-gamma sleeve stood
        down on the one regime it owns — a reintroduction of the 2026-07-21 inversion;
      * a low-confidence CHOP read was laundered into RANGE, defeating the chop veto;
      * a low-confidence TREND was laundered into RANGE, so a seller traded it.

    So the cross-check now fires ONLY when this router has landed in RANGE — i.e. we
    are about to trade the permissive default on what may be a non-signal. An
    affirmative classification (TREND/CHOP/INSIDE) is a real detection and is trusted
    as-is. When it does fire, the MORE CONSERVATIVE of the two decisions wins, so the
    fallback can only ever reduce risk, never authorize a trade the fine read refused.
    """
    fine_label = str(regime_label or "").upper()
    decision = _route_one(fine_label, confidence, specialist, notes=[])
    coarse = str(fallback_regime or "").upper()

    # (a) R1: the fine read RESOLVED to RANGE (its no-signature default). Cross-check
    # the mature coarse regime and take the MORE CONSERVATIVE — this stands a seller
    # down when coarse says TREND but the fine defaulted to RANGE (the 2026-07-22 loss).
    if (decision.regime == tax.RANGE and coarse in tax.REGIME_LABELS
            and coarse != tax.RANGE):
        note = (f"fine regime {fine_label or 'UNKNOWN'} resolved to RANGE (the "
                f"no-signature default, confidence {confidence:.2f}) — cross-checking "
                f"the mature coarse regime {coarse}")
        alt = _route_one(coarse, confidence, specialist, notes=[note])
        return _more_conservative(alt, decision)

    # (b) 2026-07-23 symmetric rescue: the fine read is a LOW-CONFIDENCE TREND that
    # stood this specialist down, while the mature coarse regime is one it DOES own.
    # A 0.03-confidence "trend" is noise — it must not suppress a validated seller
    # whose coarse home is RANGE (QG-O1 was stood down 6x this way on 2026-07-23,
    # while trend SPECIALISTS need 0.90 confidence to act — an asymmetry). Scoped to
    # TRENDS only: a low-confidence CHOP still warns of whipsaw (asymmetric downside
    # for a seller), so the chop veto is left intact. This ONLY turns an off-regime
    # stand-down into a trade; it never suppresses one, so it can only reduce
    # over-blocking, never add risk. The real 07-22 loss (coarse genuinely TREND_DOWN)
    # is unaffected — there the coarse is NOT owned, so `alt` also stands down.
    if (decision.stand_down and confidence < FINE_MIN_CONF
            and fine_label in (tax.TREND_UP, tax.TREND_DOWN)
            and coarse in tax.REGIME_LABELS and coarse != fine_label):
        note = (f"fine regime {fine_label} confidence {confidence:.2f} < {FINE_MIN_CONF} "
                f"(noise) stood {specialist} down; deferring to the mature coarse regime {coarse}")
        alt = _route_one(coarse, confidence, specialist, notes=[note])
        if not alt.stand_down:
            return alt
    return decision


def _route_one(
    regime_label: str,
    confidence: float,
    specialist: Optional[str],
    *,
    notes: List[str],
) -> RoutingDecision:
    """Route a SINGLE regime label. All the ownership/veto/sizing logic lives here so
    `route()` can evaluate the fine and coarse labels through identical rules."""
    regime = str(regime_label or "").upper()
    if regime not in tax.REGIME_LABELS:
        # coarse/unknown live regime → treat as RANGE (sellers' home), size 1
        regime = tax.RANGE
    owner = tax.REGIME_OWNER.get(regime, "range_seller")

    # 0) chop-veto override (founder-directed 2026-07-16): when RAE_CHOP_STANDDOWN is
    # off, treat a HIGH_VOL_CHOP day as a wide RANGE so the specialist's own gate
    # decides (range sellers trade on chop). EVENT is NOT overridden — it stays a
    # hard stand-down (macro fat-tail).
    # 0a) long-vol ownership of chop is decided on the TRUE label, BEFORE the chop
    # remap below. Otherwise RAE_CHOP_STANDDOWN=false rewrites the day to RANGE and
    # the long-gamma sleeve — the one structure that wants expansion — is stood down
    # for not owning RANGE. (2026-07-21: this was the IDX Long-Gamma inversion.)
    if regime == tax.HIGH_VOL_CHOP and specialist == tax.LONG_VOL_ROLE:
        base = _f("RAE_SIZE_LONGVOL", 1.0)
        return RoutingDecision(
            regime, confidence, False, base, [specialist],
            [*notes, f"{regime}: activate {specialist} at size×{base:.2f} (long-vol owns chop)"],
        )

    if regime == tax.HIGH_VOL_CHOP and not chop_standdown_enabled():
        notes.append("HIGH_VOL_CHOP veto OFF (RAE_CHOP_STANDDOWN=false) → routed as RANGE")
        regime = tax.RANGE
        owner = tax.REGIME_OWNER.get(regime, "range_seller")

    # 1) hard stand-down regimes (nothing wins / fat tails)
    if regime in (tax.HIGH_VOL_CHOP, tax.EVENT):
        return _stand_down(regime, confidence,
                           f"{regime}: stand down (no edge / fat-tail)", notes)

    # 2) specialist ownership — a strategy only trades the regime(s) it owns
    if specialist is not None:
        owns = (
            (specialist == owner)
            or (specialist == "range_seller" and regime in SELLER_OK_REGIMES)
            or (specialist in TREND_ROLES and regime in TREND_OK_REGIMES)
        )
        if not owns:
            return _stand_down(regime, confidence,
                               f"{specialist} does not own {regime} (owner={owner}) — stand down",
                               notes)

    # 3) trend needs high confidence (the 498-day study: bare trend calls are 16% precise)
    if regime in (tax.TREND_UP, tax.TREND_DOWN) and confidence < TREND_MIN_CONF:
        return _stand_down(regime, confidence,
                           f"{regime} confidence {confidence:.2f} < {TREND_MIN_CONF} — likely fakeout, stand down",
                           notes)

    base = REGIME_SIZE.get(regime, 1.0)
    # 2026-08-02 (M5): confidence-MAGNITUDE size scaling is disabled by default because
    # the score-IC screen found regime confidence is inverted vs P&L (see
    # `score_size_neutral`). The categorical gates above (off-regime stand-down, trend
    # precision) already used confidence soundly; only the continuous magnitude scaling
    # below is the "sizing on noise" part. Reversible via SCORE_SIZE_NEUTRAL=false.
    if not score_size_neutral():
        # Quiet regimes scale with conviction too. An established range earns full size;
        # a barely-formed one earns a fraction (the pre-M5 behaviour).
        if regime in SELLER_OK_REGIMES and SELLER_SIZE_CONF_REF > 0:
            base *= max(0.25, min(1.0, confidence / SELLER_SIZE_CONF_REF))
        # trend size scales with how far past the confidence gate we are (precision → size)
        if regime in (tax.TREND_UP, tax.TREND_DOWN) and TREND_MIN_CONF < 1.0:
            scale = (confidence - TREND_MIN_CONF) / (1.0 - TREND_MIN_CONF)
            base *= max(0.0, min(1.0, 0.5 + 0.5 * scale))   # 0.5..1.0 across the confident band
    active = [specialist] if specialist else [owner]
    reasons = [*notes, f"{regime}: activate {active[0]} at size×{base:.2f}"]
    return RoutingDecision(regime, confidence, False, base, active, reasons)
