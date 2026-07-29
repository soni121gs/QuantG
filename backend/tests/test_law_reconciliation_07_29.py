"""2026-07-29 law reconciliation.

The week of 07-27..29 won 81.8% of its trades (18/22) and still lost Rs1,157,
because the average win was Rs307 and the average loss Rs1,669 — a breakeven
win rate of 84.5%. The cause was not the signal: it was four independent
defects, all of which let one part of the system contradict another.

  1. The cost-floor law approved trades on `tp_frac x credit x lot` (Rs900-1,400)
     while the trailing exit banked them at a flat Rs300 arm and a 45% giveback
     (realized avg Rs284, below the trade's own friction).
  2. The exposure cap counted POSITIONS, so three strategies holding the exact
     same contract read as diversified.
  3. HIGH_VOL_CHOP — the regime the 498-day study says nothing wins in — was
     remapped to RANGE so sellers kept trading it.
  4. EdgeMath's conviction-0 stand-down was unreachable behind a 1-lot floor.
"""

import os

import pytest

from core.dynamic_exit import trailing_lock_levels
from core.spread_builder import (
    SPREAD_COST_FLOOR_MULT,
    credit_cost_floor,
    min_bankable_profit,
    round_trip_friction,
)

# The real 2026-07-27 NIFTY trade that started this: credit 39.55, lot 65,
# legs 54.95/15.40, peak Rs718, banked Rs294 on a TP target of Rs1,272.
NIFTY = dict(net_credit=39.55, lot_size=65, qty=65, legs=54.95 + 15.40, peak=718.25)


# ── the shared law ────────────────────────────────────────────────────────


def test_builder_and_exit_agree_on_the_same_floor():
    """§21.5: when a law is encoded in two places they must share the arithmetic."""
    floor = min_bankable_profit(NIFTY["lot_size"], leg_premium_sum=NIFTY["legs"], lots=1)
    friction = round_trip_friction(NIFTY["legs"], NIFTY["lot_size"])
    assert floor == pytest.approx(SPREAD_COST_FLOOR_MULT * friction)

    gate = credit_cost_floor(NIFTY["net_credit"], 200.0, lot_size=NIFTY["lot_size"],
                             tp_frac=0.5, leg_premium_sum=NIFTY["legs"])
    assert gate["required_floor"] == pytest.approx(floor)


def test_friction_is_premium_proportional_not_flat():
    """A flat constant under-charged BANKNIFTY by ~4x. Fat premium costs more."""
    cheap = min_bankable_profit(65, leg_premium_sum=20.0, lots=1)
    rich = min_bankable_profit(65, leg_premium_sum=400.0, lots=1)
    assert rich > cheap


def test_floor_scales_with_lots():
    assert min_bankable_profit(65, leg_premium_sum=70.0, lots=3) == pytest.approx(
        3 * min_bankable_profit(65, leg_premium_sum=70.0, lots=1))


# ── 1. the trail may not bank below the floor ─────────────────────────────


def test_the_real_trade_no_longer_arms_below_its_cost_floor():
    """Rs718 peak used to arm (flat Rs300 floor) and lock at Rs431 gross ->
    Rs294 realized. It must not arm below what the entry gate was promised."""
    lv = trailing_lock_levels(NIFTY["net_credit"], NIFTY["qty"], NIFTY["peak"],
                              arm_frac=0.2, giveback_frac=0.25,
                              lot_size=NIFTY["lot_size"], lots=1,
                              leg_premium_sum=NIFTY["legs"])
    floor = min_bankable_profit(NIFTY["lot_size"], leg_premium_sum=NIFTY["legs"], lots=1)
    assert lv["arm_level"] >= floor
    assert lv["floor_basis"] == "cost_floor_law"
    assert not lv["armed"], "Rs718 peak is below the floor — must not arm"


def test_once_armed_the_lock_cannot_fall_below_the_floor():
    """Arming above the floor is not enough: a 25% giveback on a peak that only
    just cleared it would bank below it again."""
    floor = min_bankable_profit(NIFTY["lot_size"], leg_premium_sum=NIFTY["legs"], lots=1)
    lv = trailing_lock_levels(NIFTY["net_credit"], NIFTY["qty"], floor + 10.0,
                              arm_frac=0.2, giveback_frac=0.25,
                              lot_size=NIFTY["lot_size"], lots=1,
                              leg_premium_sum=NIFTY["legs"])
    assert lv["armed"]
    assert lv["lock_level"] >= floor


def test_a_big_winner_still_trails_normally_above_the_floor():
    """The floor is a minimum, not a fixed target — a fat winner must still give
    back only the configured fraction, or the trail stops protecting anything."""
    lv = trailing_lock_levels(NIFTY["net_credit"], NIFTY["qty"], 2000.0,
                              arm_frac=0.2, giveback_frac=0.25,
                              lot_size=NIFTY["lot_size"], lots=1,
                              leg_premium_sum=NIFTY["legs"])
    assert lv["armed"]
    assert lv["lock_level"] == pytest.approx(1500.0)


def test_arm_level_never_exceeds_the_max_achievable_profit():
    """A floor above the whole credit would make the trail unreachable, silently
    degrading every position into hold-to-SL-or-clock."""
    lv = trailing_lock_levels(2.0, 20, 500.0, arm_frac=0.2, giveback_frac=0.25,
                              lot_size=20, lots=1, leg_premium_sum=900.0)
    assert lv["arm_level"] <= 2.0 * 20


def test_no_contract_context_falls_back_to_the_flat_floor():
    lv = trailing_lock_levels(39.55, 65, 718.25, arm_frac=0.2, giveback_frac=0.25)
    assert lv["arm_level"] is not None


def test_the_switch_reverts_to_old_behaviour(monkeypatch):
    import importlib
    import core.dynamic_exit as de

    monkeypatch.setenv("DYN_EXIT_TRAIL_HONOUR_COST_FLOOR", "false")
    importlib.reload(de)
    try:
        lv = de.trailing_lock_levels(NIFTY["net_credit"], NIFTY["qty"], NIFTY["peak"],
                                     arm_frac=0.2, giveback_frac=0.45,
                                     lot_size=NIFTY["lot_size"], lots=1,
                                     leg_premium_sum=NIFTY["legs"])
        assert lv["armed"], "with the law off, the old flat-floor behaviour returns"
        assert lv["lock_level"] == pytest.approx(718.25 * 0.55)
    finally:
        monkeypatch.delenv("DYN_EXIT_TRAIL_HONOUR_COST_FLOOR", raising=False)
        importlib.reload(de)


# ── 3. chop stand-down ────────────────────────────────────────────────────


def test_chop_is_a_stand_down_regime_again(monkeypatch):
    import importlib
    import core.regime_router as rr
    import core.regime_taxonomy as tax

    monkeypatch.setenv("RAE_ROUTER_ENABLED", "true")
    monkeypatch.setenv("RAE_CHOP_STANDDOWN", "true")
    importlib.reload(rr)
    try:
        r = rr.route(tax.HIGH_VOL_CHOP, 1.0, specialist="range_seller")
        assert r.stand_down, "sellers must not trade HIGH_VOL_CHOP"
        assert r.size_mult == 0
    finally:
        monkeypatch.delenv("RAE_CHOP_STANDDOWN", raising=False)
        monkeypatch.delenv("RAE_ROUTER_ENABLED", raising=False)
        importlib.reload(rr)


# ── 4. EdgeMath stand-down is reachable ───────────────────────────────────


def test_edge_standdown_default_on_and_reversible(monkeypatch):
    from signal_manager import _contract_dedup_enabled, _edge_standdown_enabled

    assert _edge_standdown_enabled() is True
    assert _contract_dedup_enabled() is True
    monkeypatch.setenv("EDGE_STANDDOWN_ENABLED", "false")
    monkeypatch.setenv("CONTRACT_DEDUP_ENABLED", "false")
    assert _edge_standdown_enabled() is False
    assert _contract_dedup_enabled() is False


def test_negative_expectancy_sizes_to_zero():
    """The core of #4: EdgeMath said 'stand down' on 11 of 22 trades and all 11
    traded, because paper passed floor_lots=1 and the caller applied max(1, ...)."""
    from core.edge_sizer import RollingStats, edge_size, payoff_ratio

    # The exact rolling stats stamped on the 2026-07-27 QG-O1 trade, whose own
    # telemetry read "expectancy -35.0 <= costs 0.0 -> stand down" — and traded.
    stats = RollingStats(n=22, win_rate=0.5, avg_win=282.83, avg_loss=352.87,
                         expectancy=-35.02)
    d = edge_size(stats=stats, payoff_b=payoff_ratio(stats.avg_win, stats.avg_loss),
                  equity=500000.0, per_lot_max_loss=10429.25,
                  day_pnl=0.0, daily_risk_budget=10000.0, peak_day_pnl=0.0,
                  floor_lots=0)
    assert d.lots == 0, "a negative-expectancy strategy must be able to size to zero"

    floored = edge_size(stats=stats, payoff_b=payoff_ratio(stats.avg_win, stats.avg_loss),
                        equity=500000.0, per_lot_max_loss=10429.25,
                        day_pnl=0.0, daily_risk_budget=10000.0, peak_day_pnl=0.0,
                        floor_lots=1)
    assert floored.lots == 1, "the old paper floor is what made stand-down unreachable"
