"""RES-3 — dynamic exit engine (bank + trailing lock + fast stop).

The old spread exit geometry was static: bank at a fixed % of credit, stop at a
fixed multiple, else ride to EOD. That is exactly why a green position gives back
its gain and closes red — there is no mechanism to LOCK a fading winner.

This module adds a **trailing lock**: once a position has captured a meaningful
peak profit (armed), if that profit retraces past a giveback threshold, it exits
and banks what's left instead of round-tripping to red. It is layered on top of
the existing hard take-profit / stop-loss levels and is **purely additive** — it
can only cause an EARLIER profit-taking exit, never suppress the stop or hold
longer.

PURE functions (no I/O) so the same logic runs in the live monitor and the OOS
backtester — the RES design rule. The caller tracks the running peak P&L on the
position (via `update_peak_pnl`) and passes it in each tick.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

# Arm the trailing lock once peak profit reaches this fraction of the max profit
# (max profit on a credit spread ≈ net_credit × qty, when the spread decays to 0).
TRAIL_ARM_FRAC = float(os.environ.get("DYN_EXIT_TRAIL_ARM_FRAC", "0.4"))
# Once armed, exit if profit retraces this fraction back from its peak.
TRAIL_GIVEBACK_FRAC = float(os.environ.get("DYN_EXIT_TRAIL_GIVEBACK_FRAC", "0.5"))
# Absolute ₹ floor on the arm level. On a thin-credit spread the fractional arm
# (arm_frac × credit) can be SMALLER than the round-trip friction (brokerage +
# slippage on 4 legs, ~₹250–400/lot) — arming there would "lock a profit" that is
# actually a net LOSS after costs. Never arm the trail until the peak clears real
# friction. Fat-credit spreads already arm well above this, so they're unaffected.
TRAIL_MIN_ARM_RUPEES = float(os.environ.get("DYN_EXIT_MIN_ARM_RUPEES", "300"))


def update_peak_pnl(prev_peak: Optional[float], current_pnl: float) -> float:
    """Running maximum favourable P&L (money) seen on a position. The caller
    persists this on the position doc and feeds it back next tick."""
    cur = float(current_pnl)
    if prev_peak is None:
        return cur
    return max(float(prev_peak), cur)


def trailing_lock_levels(
    net_credit: float,
    qty: int,
    peak_pnl: Optional[float],
    *,
    arm_frac: float = TRAIL_ARM_FRAC,
    giveback_frac: float = TRAIL_GIVEBACK_FRAC,
) -> Dict[str, Any]:
    """Return {armed, arm_level, lock_level} for diagnostics / UI.

    arm_level  = arm_frac × (net_credit × qty)   — peak profit needed to arm
    lock_level = peak_pnl × (1 − giveback_frac)   — exit if current P&L falls here
    """
    credit_money = float(net_credit) * int(qty)
    # arm at the fraction of max credit, but never below real round-trip friction —
    # banking a "profit" smaller than the cost of the exit is a net loss.
    arm_level = max(arm_frac * credit_money, TRAIL_MIN_ARM_RUPEES) if credit_money > 0 else None
    armed = (arm_level is not None and peak_pnl is not None and peak_pnl >= arm_level)
    lock_level = (float(peak_pnl) * (1.0 - giveback_frac)) if armed else None
    return {"armed": bool(armed), "arm_level": arm_level, "lock_level": lock_level}


def evaluate_spread_exit(
    *,
    position: Dict[str, Any],
    current_value: float,
    current_pnl: float,
    peak_pnl: Optional[float],
    arm_frac: float = TRAIL_ARM_FRAC,
    giveback_frac: float = TRAIL_GIVEBACK_FRAC,
) -> Optional[str]:
    """Exit reason for a credit spread, or None to hold. Priority:

      1. `spread-sl`   — hard stop (loss): current spread value ≥ sl_value
      2. `spread-tp`   — take-profit target: current value ≤ tp_value
      3. `trail-lock`  — armed AND profit retraced past the giveback (bank the
                         fading winner before it round-trips to red)

    1 and 2 reproduce the existing `spread_exit_reason` exactly (so behaviour is
    unchanged when trailing doesn't fire); 3 is the new dynamic layer.
    """
    tp_value = position.get("spread_tp_value")
    sl_value = position.get("spread_sl_value")

    # 1) hard stop first — never let the trailing layer mask a loss stop
    if sl_value is not None and float(current_value) >= float(sl_value):
        return "spread-sl"
    # 2) take-profit target
    if tp_value is not None and float(current_value) <= float(tp_value):
        return "spread-tp"

    # 3) trailing lock
    net_credit = float(position.get("net_credit") or position.get("average_buy_price") or 0.0)
    qty = int(position.get("open_quantity") or position.get("quantity") or 0)
    lv = trailing_lock_levels(net_credit, qty, peak_pnl,
                              arm_frac=arm_frac, giveback_frac=giveback_frac)
    if lv["armed"] and float(current_pnl) <= float(lv["lock_level"]):
        return "trail-lock"
    return None
