"""RAE-5 (2026-07-10): regime-aware EXITS. Same principle as the router — the right
action depends on the regime. A TREND position wants a WIDE trail (let the winner
run; don't lock a fading blip that's actually a pause in a trend), while a RANGE /
INSIDE seller wants a TIGHT trail (book the theta fast before the range flips it to
red — the 2026-07-10 lesson where green round-tripped to red).

This only tunes the trailing-lock arm/giveback fractions the dynamic exit engine
(RES-3, `dynamic_exit.evaluate_spread_exit`) already accepts — the hard SL and TP
are unchanged, so a position is never made LESS safe. Pure; env-tunable.

Gated with the rest of the ensemble: when `RAE_ROUTER_ENABLED` is false the helper
returns the existing global defaults → behaviour is byte-identical to today.
"""
from __future__ import annotations

import os
from typing import Dict

from core import regime_taxonomy as tax
from core.dynamic_exit import TRAIL_ARM_FRAC, TRAIL_GIVEBACK_FRAC


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# (arm_frac, giveback_frac) per regime.
#   TREND  → arm later, give back MORE before locking = wide trail (let it run)
#   RANGE  → arm sooner, give back LESS = tight trail (book theta fast)
#   INSIDE → tightest (moves are tiny; lock the moment it fades)
REGIME_EXIT = {
    tax.TREND_UP:      {"arm_frac": _f("RAE_EXIT_TREND_ARM", 0.50), "giveback_frac": _f("RAE_EXIT_TREND_GIVEBACK", 0.70)},
    tax.TREND_DOWN:    {"arm_frac": _f("RAE_EXIT_TREND_ARM", 0.50), "giveback_frac": _f("RAE_EXIT_TREND_GIVEBACK", 0.70)},
    tax.RANGE:         {"arm_frac": _f("RAE_EXIT_RANGE_ARM", 0.35), "giveback_frac": _f("RAE_EXIT_RANGE_GIVEBACK", 0.40)},
    tax.INSIDE_QUIET:  {"arm_frac": _f("RAE_EXIT_INSIDE_ARM", 0.30), "giveback_frac": _f("RAE_EXIT_INSIDE_GIVEBACK", 0.35)},
}


def _enabled() -> bool:
    return os.environ.get("RAE_ROUTER_ENABLED", "false").lower() == "true"


def regime_exit_params(regime_label: str, *, enabled: bool = None) -> Dict[str, float]:
    """Trailing-lock (arm_frac, giveback_frac) for the position's entry regime. When
    the ensemble is disabled, or the regime is unknown, returns the existing global
    defaults so the exit path is a byte-for-byte no-op."""
    on = _enabled() if enabled is None else enabled
    default = {"arm_frac": TRAIL_ARM_FRAC, "giveback_frac": TRAIL_GIVEBACK_FRAC}
    if not on:
        return default
    return REGIME_EXIT.get(str(regime_label or "").upper(), default)
