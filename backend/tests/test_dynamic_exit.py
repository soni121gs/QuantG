"""Tests for RES-3 — core/dynamic_exit.py (bank + trailing lock + fast stop)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.dynamic_exit import (
    update_peak_pnl,
    trailing_lock_levels,
    evaluate_spread_exit,
)


def _pos(net_credit=20.0, qty=65, tp=10.0, sl=50.0):
    return {"net_credit": net_credit, "open_quantity": qty, "quantity": qty,
            "spread_tp_value": tp, "spread_sl_value": sl}


# ── peak tracking ───────────────────────────────────────────────────────────────

def test_update_peak_pnl():
    assert update_peak_pnl(None, 100.0) == 100.0
    assert update_peak_pnl(100.0, 40.0) == 100.0   # peak holds when P&L fades
    assert update_peak_pnl(100.0, 150.0) == 150.0   # peak rises with a new high


# ── arm levels ──────────────────────────────────────────────────────────────────

def test_trailing_lock_levels_arms_at_fraction_of_max_profit():
    # credit_money = 20*65 = 1300; arm at 0.4 → 520.
    not_armed = trailing_lock_levels(20.0, 65, peak_pnl=400.0)
    armed = trailing_lock_levels(20.0, 65, peak_pnl=650.0)
    assert not_armed["armed"] is False and not_armed["lock_level"] is None
    assert armed["armed"] is True
    assert armed["arm_level"] == 0.4 * 1300
    assert armed["lock_level"] == 650.0 * 0.5   # giveback 0.5


# ── exit priority ───────────────────────────────────────────────────────────────

def test_hard_stop_wins_even_with_high_peak():
    # value 55 ≥ sl 50 → stop, regardless of a great peak.
    r = evaluate_spread_exit(position=_pos(), current_value=55.0,
                             current_pnl=-2275.0, peak_pnl=800.0)
    assert r == "spread-sl"


def test_take_profit_target():
    # value 8 ≤ tp 10 → take-profit.
    r = evaluate_spread_exit(position=_pos(), current_value=8.0,
                             current_pnl=780.0, peak_pnl=780.0)
    assert r == "spread-tp"


def test_trailing_lock_banks_a_faded_winner():
    # Armed (peak 650 ≥ 520). Value 15 → pnl (20-15)*65 = 325 = lock level
    # (650*0.5). Between tp 10 and sl 50, so no hard exit → trail-lock fires.
    r = evaluate_spread_exit(position=_pos(), current_value=15.0,
                             current_pnl=325.0, peak_pnl=650.0)
    assert r == "trail-lock"


def test_no_lock_when_not_armed():
    # Same fade, but peak 400 < arm 520 → not armed → hold (matches old behaviour).
    r = evaluate_spread_exit(position=_pos(), current_value=15.0,
                             current_pnl=325.0, peak_pnl=400.0)
    assert r is None


def test_hold_when_armed_but_still_near_peak():
    # Armed (peak 520) but current pnl 520 well above lock 260 → keep riding.
    r = evaluate_spread_exit(position=_pos(), current_value=12.0,
                             current_pnl=520.0, peak_pnl=520.0)
    assert r is None


def test_parity_with_static_when_trailing_inactive():
    # Peak None / no arming → behaves exactly like the old tp/sl-only check.
    assert evaluate_spread_exit(position=_pos(), current_value=30.0,
                                current_pnl=-650.0, peak_pnl=None) is None
    assert evaluate_spread_exit(position=_pos(), current_value=50.0,
                                current_pnl=-1950.0, peak_pnl=None) == "spread-sl"
    assert evaluate_spread_exit(position=_pos(), current_value=10.0,
                                current_pnl=650.0, peak_pnl=None) == "spread-tp"
