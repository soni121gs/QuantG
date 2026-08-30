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

# Universal profit-protection layer. This is deliberately separate from the
# normal trailing lock: it protects any spread that has already been meaningfully
# green from round-tripping into a red close, including hold-to-expiry spreads
# that otherwise bypass intraday TP/SL/trailing.
PROTECT_GREEN_ENABLED = os.environ.get(
    "DYN_EXIT_PROTECT_GREEN_ENABLED", "true").strip().lower() == "true"
PROTECT_GREEN_ARM_FRAC = float(os.environ.get("DYN_EXIT_PROTECT_GREEN_ARM_FRAC", "0.15"))
PROTECT_GREEN_GIVEBACK_FRAC = float(os.environ.get("DYN_EXIT_PROTECT_GREEN_GIVEBACK_FRAC", "0.70"))
PROTECT_GREEN_MIN_RUPEES = float(os.environ.get("DYN_EXIT_PROTECT_GREEN_MIN_RUPEES", "300"))
PROTECT_GREEN_MIN_LOCK_RUPEES = float(os.environ.get("DYN_EXIT_PROTECT_GREEN_MIN_LOCK_RUPEES", "50"))

# ── "never went green" early cut (2026-07-30) ────────────────────────────────
# Measured over 94 closed spreads: 54% of LOSERS were never meaningfully green
# (peak <= Rs50) — they go straight against the position. And `spread-time-exit`
# (38% of all trades) had a median peak of 0.3% of credit: dead on arrival, then
# left to bleed to the time bell or the stop. A theta winner, by contrast, is
# nearest its peak EARLY (winners' median peak 29% of credit). So a spread that
# has shown NO life after a fair window is overwhelmingly a loser being held in
# hope — cut it, free the slot, stop the bleed. This is the disciplined opposite
# of averaging down (which nearly TRIPLES the book's loss on its own history).
NO_PROGRESS_ENABLED = os.environ.get("DYN_EXIT_NO_PROGRESS_ENABLED", "true").strip().lower() == "true"
NO_PROGRESS_MINUTES = float(os.environ.get("DYN_EXIT_NO_PROGRESS_MINUTES", "20"))
# Peak must clear this fraction of max credit within the window, or the trade is
# judged lifeless. 8% cleanly separates: winners median-peak at 29%, dead trades
# at ~0.3%. Also floored at real friction so a thin-credit spread isn't cut for
# failing to clear a rupee bar it never could.
NO_PROGRESS_PEAK_FRAC = float(os.environ.get("DYN_EXIT_NO_PROGRESS_PEAK_FRAC", "0.08"))
# Only apply the bar once decay could plausibly have cleared it — see the long
# note in no_progress_exit(). Env-reversible to restore the flat-window behaviour.
NO_PROGRESS_REQUIRE_REACHABLE = (
    os.environ.get("DYN_EXIT_NO_PROGRESS_REQUIRE_REACHABLE", "true").strip().lower() == "true")
try:
    from core.spread_builder import MARKET_MINUTES_PER_DAY
except Exception:  # pragma: no cover - keeps this module import-safe standalone
    MARKET_MINUTES_PER_DAY = float(os.environ.get("MARKET_MINUTES_PER_DAY", "385"))


def no_progress_exit(
    *,
    peak_pnl: Optional[float],
    held_minutes: Optional[float],
    net_credit: float,
    qty: int,
    lot_size: Optional[int] = None,
    lots: int = 1,
    leg_premium_sum: Optional[float] = None,
    dte_days: Optional[float] = None,
    session_minutes: Optional[float] = None,
) -> Optional[str]:
    """Return "spread-no-progress" if a spread has shown no life within the
    window, else None. Pure. Priced exits (SL/TP/trail) must be checked FIRST by
    the caller so a trade that is actually working is never cut.

    2026-08-03 — THE BAR MUST BE REACHABLE BEFORE IT IS APPLIED.
    The 8%-of-credit bar was calibrated against the peak distribution of trades
    measured over their WHOLE hold (winners median-peak 29%, dead trades ~0.3%),
    then applied inside a flat 20-minute window. Those are different questions.
    Theta can only deliver `held / (dte x session)` of the position's remaining
    time value, so in 20 minutes it supplies ~5.2% at 0-1 DTE but only 1.3% at
    4 DTE and 0.87% at 6 DTE — while the bar asks for 8%.

    Measured on the 30 post-re-cut trades this rule closed: entries sat mostly at
    DTE 4/6/25, mean peak was 3.40% of credit, and ZERO of 30 ever cleared 8%.
    The rule was not separating dead trades from live ones — it was cutting
    everything that had not been directionally lucky in its first 20 minutes, at
    an average of -Rs127 a trade. Same defect class as §21.5/§22.3: a threshold
    measured in one regime applied to another.

    Fix: keep the bar, but do not JUDGE until enough decay has been available for
    the bar to be clearable. A trade is then cut for failing a test it could have
    passed. Near expiry that is still ~20-30 minutes; at 6 DTE it waits hours,
    which is the correct answer for a position whose theta has not arrived yet.
    """
    if not NO_PROGRESS_ENABLED:
        return None
    if held_minutes is None or held_minutes < NO_PROGRESS_MINUTES:
        return None
    credit_money = float(net_credit) * int(qty or 0)
    if credit_money <= 0:
        return None
    # life threshold = max(fraction of credit, real round-trip friction)
    floor = NO_PROGRESS_PEAK_FRAC * credit_money
    if lot_size:
        try:
            from core.spread_builder import round_trip_friction
            floor = max(floor, round_trip_friction(leg_premium_sum, lot_size) * max(1, int(lots or 1)))
        except Exception:
            pass
    # Reachability gate: how much of the position's time value could decay
    # plausibly have returned by now? Below the bar, the trade has not yet been
    # given the chance to pass, so judging it is judging noise.
    #
    # FAILS CLOSED (2026-08-04). This gate previously read
    # `... and dte_days is not None`, so an unresolvable DTE skipped the gate and
    # restored the flat 20-minute window this fix exists to remove. That is what
    # happened the day after it shipped: the caller parsed a bare-date expiry into
    # a NAIVE datetime, subtracting it from an aware utcnow raised TypeError, a
    # bare `except` swallowed it, `dte_days` arrived as None — and all 8 spreads
    # that day were cut at exactly 20 minutes for -Rs2,337, with the guard
    # deployed, enabled and completely inert.
    # An input we cannot resolve must disable the RULE, never the SAFEGUARD: if we
    # cannot tell whether the bar was reachable, we have no business judging the
    # trade against it.
    # A floor at or above the ENTIRE credit can never be cleared by decay at any
    # hold — the spread cannot pay for its own round trip even if it goes to zero.
    # That is not "not yet judgeable", it is hopeless by construction, so the
    # reachability gate does not apply and the cut stands. (Such a spread should
    # never have been opened; that is the §21.1 cost floor's job at entry.)
    _unpayable = floor >= credit_money
    if NO_PROGRESS_REQUIRE_REACHABLE and not _unpayable:
        if dte_days is None:
            return None
        try:
            _dte = max(0.0, float(dte_days))
            _sess = float(session_minutes or MARKET_MINUTES_PER_DAY)
            _life = max(_sess, (_dte if _dte > 0 else 1.0) * _sess)
            available = min(1.0, float(held_minutes) / _life) * credit_money
            if available < floor:
                return None
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    if (peak_pnl or 0.0) < floor:
        return "spread-no-progress"
    return None


def update_peak_pnl(prev_peak: Optional[float], current_pnl: float) -> float:
    """Running maximum favourable P&L (money) seen on a position. The caller
    persists this on the position doc and feeds it back next tick."""
    cur = float(current_pnl)
    if prev_peak is None:
        return cur
    return max(float(prev_peak), cur)


def _spread_profit_capacity(position: Dict[str, Any]) -> float:
    qty = int(position.get("open_quantity") or position.get("quantity") or 0)
    if qty <= 0:
        return 0.0
    structure = str(position.get("structure") or "")
    if structure == "debit_spread":
        net_debit = float(position.get("net_debit") or position.get("average_buy_price") or 0.0)
        width = float(position.get("max_loss") or 0.0) + net_debit
        return max(0.0, width - net_debit) * qty
    net_credit = float(position.get("net_credit") or position.get("average_buy_price") or 0.0)
    return max(0.0, net_credit) * qty


def green_profit_protection_levels(
    position: Dict[str, Any],
    peak_pnl: Optional[float],
    *,
    arm_frac: float = PROTECT_GREEN_ARM_FRAC,
    giveback_frac: float = PROTECT_GREEN_GIVEBACK_FRAC,
) -> Dict[str, Any]:
    """Return the universal green-to-red guard levels for a spread position."""
    capacity = _spread_profit_capacity(position)
    if capacity <= 0:
        return {"armed": False, "arm_level": None, "lock_level": None,
                "capacity": capacity, "reason": "no_capacity"}
    arm_level = min(capacity, max(PROTECT_GREEN_MIN_RUPEES, capacity * float(arm_frac)))
    armed = peak_pnl is not None and float(peak_pnl) >= arm_level
    lock_level = None
    if armed:
        lock_level = max(PROTECT_GREEN_MIN_LOCK_RUPEES,
                         float(peak_pnl) * (1.0 - float(giveback_frac)))
        lock_level = min(lock_level, float(peak_pnl))
    return {"armed": bool(armed), "arm_level": arm_level, "lock_level": lock_level,
            "capacity": capacity, "reason": "armed" if armed else "not_armed"}


def green_profit_protection_exit(
    *,
    position: Dict[str, Any],
    current_pnl: float,
    peak_pnl: Optional[float],
    arm_frac: float = PROTECT_GREEN_ARM_FRAC,
    giveback_frac: float = PROTECT_GREEN_GIVEBACK_FRAC,
) -> Optional[str]:
    """Protect a meaningful open profit from becoming a red close.

    The caller should check hard take-profit first, then this guard, then hard
    stop/no-progress. If a tick jumps straight through the lock into red, this
    still closes immediately rather than waiting for the full stop.
    """
    if not PROTECT_GREEN_ENABLED:
        return None
    lv = green_profit_protection_levels(
        position, peak_pnl, arm_frac=arm_frac, giveback_frac=giveback_frac)
    if lv["armed"] and float(current_pnl) <= float(lv["lock_level"]):
        return "profit-protect"
    return None


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

      1. `spread-tp`   — take-profit target: current value ≤ tp_value
      2. `profit-protect` — was meaningfully green, now retraced to lock level
      3. `spread-sl`   — hard stop (loss): current spread value ≥ sl_value
      4. `trail-lock`  — armed AND profit retraced past the giveback (bank the
                         fading winner before it round-trips to red)

    The profit-protection layers sit before full stop-loss liquidation so a
    winner that fades does not need to become a full loser before exit.
    """
    tp_value = position.get("spread_tp_value")
    sl_value = position.get("spread_sl_value")

    # 1) take-profit target
    if tp_value is not None and float(current_value) <= float(tp_value):
        return "spread-tp"
    # 2) universal green-to-red guard. This may fire before the hard stop, because
    # a trade that already earned a meaningful profit should not wait for the full
    # stop just because it missed the formal TP by a tick.
    protected = green_profit_protection_exit(
        position=position, current_pnl=current_pnl, peak_pnl=peak_pnl)
    if protected:
        return protected
    # 3) hard stop
    if sl_value is not None and float(current_value) >= float(sl_value):
        return "spread-sl"

    # 4) trailing lock
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
