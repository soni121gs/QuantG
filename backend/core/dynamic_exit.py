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
# 2026-07-29 — THE LAW RECONCILIATION. The flat ₹300 above only covers ONE round
# trip; the cost-floor law (§21.1) approves a trade only when it can bank
# SPREAD_COST_FLOOR_MULT (3x) that. Arming at ₹300 and then giving back 45% of the
# peak banked ~₹284 on average across the 22 trades of 2026-07-27..29 — below the
# friction of the trade itself, while the losing side paid a full ~₹1,669 stop.
# 82% of trades won and the book still lost, because breakeven needed 84.5%.
# The trail must not bank below what the entry gate was promised. Set false to
# revert to the pre-2026-07-29 flat floor.
TRAIL_HONOUR_COST_FLOOR = os.environ.get(
    "DYN_EXIT_TRAIL_HONOUR_COST_FLOOR", "true").strip().lower() == "true"
# Multiple of REAL (premium-proportional) round-trip friction the trail must clear
# before it may arm.
#
# NOT the cost-floor law's 3x. That 3x is an EX-ANTE criterion — "is this structure
# worth taking at all" — and applying it per-exit is a category error: simulated
# against the 22 real trades of 2026-07-27..29 a 3x arm floor armed on 1 of 22,
# which does not tighten the trail, it DELETES it and sends every position to
# TP/SL/clock (the 2026-07-10 round-trip-to-red failure). The ex-post question is
# narrower: does this exit bank meaningfully more than it cost to place?
# 1.5x measured as the point where the trail still fires on most winners while the
# banked amount clears friction with margin.
TRAIL_ARM_COST_MULT = float(os.environ.get("DYN_EXIT_TRAIL_ARM_COST_MULT", "1.5"))


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
    lot_size: Optional[int] = None,
    lots: int = 1,
    leg_premium_sum: Optional[float] = None,
) -> Dict[str, Any]:
    """Return {armed, arm_level, lock_level, floor_basis} for diagnostics / UI.

    arm_level  = max(arm_frac × credit_money, cost-floor bankable profit)
    lock_level = peak_pnl × (1 − giveback_frac)   — exit if current P&L falls here

    The cost-floor term is the reconciliation described at TRAIL_HONOUR_COST_FLOOR:
    the trail may never arm below the profit the entry gate was promised, because
    a lock below that banks less than the trade cost to place. When the caller has
    no contract context the flat TRAIL_MIN_ARM_RUPEES remains the lower bound.
    """
    credit_money = float(net_credit) * int(qty)
    if credit_money <= 0:
        return {"armed": False, "arm_level": None, "lock_level": None, "floor_basis": "no_credit"}

    floor = TRAIL_MIN_ARM_RUPEES
    basis = "flat_friction"
    # Only bind the cost-floor law when the REAL contract is known. Without a
    # lot size there is no friction model, and multiplying a guessed constant by
    # 3 could push the arm out of reach — which would silently degrade every
    # position into hold-to-SL-or-clock, the exact failure being fixed here.
    if TRAIL_HONOUR_COST_FLOOR and lot_size:
        try:
            from core.spread_builder import round_trip_friction
            friction = round_trip_friction(leg_premium_sum, lot_size) * max(1, int(lots or 1))
            cost_floor = TRAIL_ARM_COST_MULT * friction
            if cost_floor > floor:
                floor, basis = cost_floor, "real_friction"
        except Exception:
            pass  # fail OPEN to the old flat floor — never block an exit on import

    # Never demand more than the position can actually produce: the whole credit is
    # the ceiling, and a trail that can never arm silently degrades into "hold until
    # SL or the clock", which is the failure mode this is fixing.
    arm_level = min(max(arm_frac * credit_money, floor), credit_money)
    if arm_level >= credit_money:
        basis = "capped_at_max_profit"
    armed = (peak_pnl is not None and float(peak_pnl) >= arm_level)
    lock_level = None
    if armed:
        lock_level = float(peak_pnl) * (1.0 - giveback_frac)
        # Arming above the floor is not enough on its own: giving back 45% of a
        # peak that only just cleared the floor banks LESS than the floor again.
        # The retrace target is clamped to the same number, so once the trail is
        # armed the trade cannot be cashed out below what it cost to place.
        if TRAIL_HONOUR_COST_FLOOR:
            lock_level = max(lock_level, min(floor, float(peak_pnl)))
    return {"armed": bool(armed), "arm_level": arm_level,
            "lock_level": lock_level, "floor_basis": basis}


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
    # Contract context so the arm floor can be the real (premium-proportional)
    # friction rather than the flat constant — BANKNIFTY's true round trip is
    # ~4.2x the constant, so the flat floor under-charged it worst of all.
    legs = position.get("legs") or []
    leg_premium_sum = None
    try:
        prem = [float(l.get("entry_price") or l.get("premium") or 0) for l in legs]
        leg_premium_sum = sum(prem) if any(prem) else None
    except Exception:
        leg_premium_sum = None
    lv = trailing_lock_levels(net_credit, qty, peak_pnl,
                              arm_frac=arm_frac, giveback_frac=giveback_frac,
                              lot_size=position.get("lot_size"),
                              lots=int(position.get("lots") or 1),
                              leg_premium_sum=leg_premium_sum)
    if lv["armed"] and float(current_pnl) <= float(lv["lock_level"]):
        return "trail-lock"
    return None
