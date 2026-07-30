"""DTE-conditional seller policy (2026-07-30).

Founder ask: MORE trades, but good ones — "relax both but in the right way, not
generalized". The blanket CHOP stand-down and the blanket EdgeMath zero-size were
both mis-aimed. Measured over 259 real closed credit spreads, days-to-expiry is the
dimension that actually separates outcomes, and it reframes the chop question:

    HIGH_VOL_CHOP DTE 0    n=7   WR 100%  avg  +Rs391
    HIGH_VOL_CHOP DTE 1-2  n=15  WR  60%  avg  -Rs299
    HIGH_VOL_CHOP DTE 3+   n=6   WR   0%  avg -Rs1143

Chop is not the enemy; far expiry is. So: trade every regime at DTE 0-1 (the best
bucket, which a flat 3x cost floor was vetoing outright), owned regimes only at
DTE 2, and stand down past that (147 trades, WR 23-41%).
"""

from datetime import date, timedelta

import pytest

from core import dte_policy as dp


TODAY = date(2026, 7, 30)


def _exp(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


# ── the DTE gate ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("regime", ["RANGE", "INSIDE_QUIET", "HIGH_VOL_CHOP",
                                    "TREND_UP", "TREND_DOWN", None])
def test_near_expiry_allows_every_regime_including_chop(regime):
    """The whole point of the change: DTE 0-1 is the best measured bucket, so no
    regime veto applies there. CHOP@DTE0 was +Rs391 at 100% WR."""
    for d in (0, 1):
        p = dp.evaluate(expiry=_exp(d), regime=regime, today=TODAY, enabled=True)
        assert p.allow, f"{regime} at DTE {d} must trade"
        assert p.near_expiry
        assert p.cost_floor_mult == dp.NEAR_EXPIRY_COST_FLOOR_MULT
        assert p.disable_time_exit


def test_dte2_allows_owned_regimes_only():
    assert dp.evaluate(expiry=_exp(2), regime="RANGE", today=TODAY, enabled=True).allow
    assert dp.evaluate(expiry=_exp(2), regime="INSIDE_QUIET", today=TODAY, enabled=True).allow
    blocked = dp.evaluate(expiry=_exp(2), regime="HIGH_VOL_CHOP", today=TODAY, enabled=True)
    assert not blocked.allow
    assert "not seller-owned" in blocked.reason


def test_dte2_owned_regime_gets_no_near_expiry_exemption():
    p = dp.evaluate(expiry=_exp(2), regime="RANGE", today=TODAY, enabled=True)
    assert p.allow and not p.near_expiry
    assert p.cost_floor_mult is None and not p.disable_time_exit


@pytest.mark.parametrize("d", [3, 5, 7, 14])
@pytest.mark.parametrize("regime", ["RANGE", "INSIDE_QUIET", "HIGH_VOL_CHOP", "TREND_UP"])
def test_far_expiry_stands_down_in_every_regime(d, regime):
    """DTE 3+ is where the money went: 147 trades, WR 23-41%, avg -Rs121..-Rs1143.
    Not even RANGE survives it."""
    p = dp.evaluate(expiry=_exp(d), regime=regime, today=TODAY, enabled=True)
    assert not p.allow
    assert "past the seller's edge window" in p.reason


def test_expired_contract_refused():
    assert not dp.evaluate(expiry=_exp(-1), regime="RANGE", today=TODAY, enabled=True).allow


def test_unknown_expiry_allows_but_grants_no_exemption():
    """Fail-open on parsing (the geometry laws still judge the contract), but an
    unknown expiry must never be treated as DTE 0 and handed the relaxed floor."""
    p = dp.evaluate(expiry=None, regime="RANGE", today=TODAY, enabled=True)
    assert p.allow
    assert not p.near_expiry
    assert p.cost_floor_mult is None


def test_disabled_policy_is_a_no_op():
    p = dp.evaluate(expiry=_exp(9), regime="HIGH_VOL_CHOP", today=TODAY, enabled=False)
    assert p.allow and not p.near_expiry


# ── DTE parsing + expiry preference ───────────────────────────────────────


def test_dte_parsing_formats():
    assert dp.dte_from_expiry("2026-08-04", today=TODAY) == 5
    assert dp.dte_from_expiry(date(2026, 8, 4), today=TODAY) == 5
    assert dp.dte_from_expiry("garbage", today=TODAY) is None
    assert dp.dte_from_expiry("", today=TODAY) is None


def test_nearest_expiry_is_preferred():
    """`target_dte_days` is decorative (§21.5) so expiries arrived in chain order.
    Sorting by DTE puts the measured-best bucket first with no config change."""
    got = dp.nearest_expiry_first([_exp(7), _exp(0), _exp(28), _exp(1)], today=TODAY)
    assert got == [_exp(0), _exp(1), _exp(7), _exp(28)]


def test_unparseable_expiry_sorts_last():
    got = dp.nearest_expiry_first(["nonsense", _exp(2)], today=TODAY)
    assert got[0] == _exp(2)


# ── the cost-floor relaxation it drives ───────────────────────────────────


def test_near_expiry_relaxes_the_cost_floor_but_never_below_friction():
    from core.spread_builder import credit_cost_floor, round_trip_friction

    legs, lot = 70.35, 65
    strict = credit_cost_floor(12.0, 200.0, lot_size=lot, tp_frac=0.45,
                               leg_premium_sum=legs)
    relaxed = credit_cost_floor(12.0, 200.0, lot_size=lot, tp_frac=0.45,
                                leg_premium_sum=legs,
                                cost_floor_mult=dp.NEAR_EXPIRY_COST_FLOOR_MULT)
    assert relaxed["required_floor"] < strict["required_floor"]
    assert relaxed["required_floor"] >= round_trip_friction(legs, lot), \
        "relaxing must never allow banking below one round trip"


def test_cost_floor_mult_is_clamped_at_one():
    from core.spread_builder import credit_cost_floor, round_trip_friction

    out = credit_cost_floor(12.0, 200.0, lot_size=65, tp_frac=0.45,
                            leg_premium_sum=70.35, cost_floor_mult=0.1)
    assert out["required_floor"] >= round_trip_friction(70.35, 65)


def test_concurrency_raised_above_one():
    from signal_manager import _max_concurrent_spreads

    assert _max_concurrent_spreads() >= 2


# ── the width narrowing that actually unlocks DTE 0 ───────────────────────


def test_near_expiry_narrows_the_wing():
    """Relaxing the cost-floor multiple alone does NOT unlock DTE 0: the
    credit/width ratio test fails independently and correctly. Observed live
    2026-07-30: SENSEX credit 33.10 on width 800 -> ratio 0.041 vs 0.120 min."""
    p = dp.evaluate(expiry=_exp(0), regime="RANGE", today=TODAY, enabled=True)
    assert p.width_strikes == dp.NEAR_EXPIRY_WIDTH_STRIKES
    assert p.width_strikes <= 3, "a 0-DTE credit cannot fill a 6-8 strike wing"


def test_far_expiry_keeps_the_configured_wing():
    p = dp.evaluate(expiry=_exp(2), regime="RANGE", today=TODAY, enabled=True)
    assert p.width_strikes is None


def test_narrow_wing_makes_a_real_0dte_credit_lawful():
    """The live SENSEX numbers: credit 33.10, lot 20, strike interval 100. On the
    configured 8-strike wing the ratio law refuses it; on 2 strikes it passes."""
    from core.spread_builder import CREDIT_SPREAD_MIN_CREDIT_RATIO, credit_cost_floor

    wide = credit_cost_floor(33.10, 800.0, lot_size=20, tp_frac=0.45,
                             leg_premium_sum=327.0)
    assert not wide["ratio_passed"]
    assert wide["credit_ratio"] < CREDIT_SPREAD_MIN_CREDIT_RATIO

    narrow = credit_cost_floor(33.10, 200.0, lot_size=20, tp_frac=0.45,
                               leg_premium_sum=327.0,
                               cost_floor_mult=dp.NEAR_EXPIRY_COST_FLOOR_MULT)
    assert narrow["ratio_passed"], "2-strike wing must clear the ratio law"


# ── the credit-ratio CEILING (added same day, after live evidence) ─────────


def test_near_expiry_sets_a_credit_ratio_ceiling():
    p = dp.evaluate(expiry=_exp(0), regime="RANGE", today=TODAY, enabled=True)
    assert p.max_credit_ratio == dp.NEAR_EXPIRY_MAX_CREDIT_RATIO
    assert p.max_credit_ratio <= 0.22
    assert p.width_strikes == 3, "2 strikes forced ATM on SENSEX; 3 does not"


def test_far_expiry_has_no_ceiling():
    assert dp.evaluate(expiry=_exp(2), regime="RANGE", today=TODAY,
                       enabled=True).max_credit_ratio is None


def test_the_three_live_atm_spreads_would_now_be_vetoed():
    """The real 2026-07-30 SENSEX spreads: credit 50.31/48.18/51.58 on width 200
    -> ratio 0.252/0.241/0.258, short strikes AT spot. All three lost immediately.
    DTE-0 ratio >=0.22 measured WR 50%, avg -Rs206."""
    from core.spread_builder import credit_cost_floor

    for credit in (50.31, 48.18, 51.58):
        out = credit_cost_floor(credit, 200.0, lot_size=20, tp_frac=0.45,
                                leg_premium_sum=129.0,
                                cost_floor_mult=dp.NEAR_EXPIRY_COST_FLOOR_MULT,
                                max_credit_ratio=dp.NEAR_EXPIRY_MAX_CREDIT_RATIO)
        assert not out["ratio_max_passed"], f"credit {credit} on width 200 is ATM"
        assert not out["passed"]


def test_the_same_credit_on_a_3_strike_wing_is_lawful():
    """Same credit, wider wing -> ratio 0.167, inside the measured-best band, and
    it still clears the relaxed absolute floor."""
    from core.spread_builder import credit_cost_floor

    out = credit_cost_floor(50.31, 300.0, lot_size=20, tp_frac=0.45,
                            leg_premium_sum=129.0,
                            cost_floor_mult=dp.NEAR_EXPIRY_COST_FLOOR_MULT,
                            max_credit_ratio=dp.NEAR_EXPIRY_MAX_CREDIT_RATIO)
    assert out["ratio_max_passed"] and out["ratio_passed"] and out["floor_passed"]
    assert out["passed"]
    assert 0.10 <= out["credit_ratio"] <= 0.22


def test_ceiling_is_off_by_default_book_wide():
    """Only the DTE policy turns it on — a far-expiry spread must be unaffected."""
    from core.spread_builder import credit_cost_floor

    out = credit_cost_floor(50.31, 200.0, lot_size=20, tp_frac=0.45,
                            leg_premium_sum=129.0)
    assert out["ratio_max_passed"]


def test_veto_reason_names_the_ATM_problem():
    """A stand-down must be diagnosable (§22.6) — the reason has to say WHY, not
    read as a generic cost_floor. Uses the real chain-node shape (instrument_key
    is required by pick_delta_strike)."""
    from core.spread_builder import build_credit_spread

    def _node(strike, ltp, delta):
        return {"strike_price": strike, "expiry": _exp(0),
                "put_options": {"instrument_key": f"BSE_FO|{strike}",
                                "market_data": {"ltp": ltp, "oi": 2_000_000},
                                "option_greeks": {"delta": delta, "theta": -30, "iv": 14}}}

    # ATM short (delta 0.50) with a 200-wide wing -> credit 50.31, ratio 0.252.
    nodes = [_node(77600, 88.66, -0.50), _node(77400, 38.35, -0.20)]
    out = build_credit_spread(chain_nodes=nodes, direction="bullish", width_points=200,
                              short_delta=0.50, lot_size=20, tp_frac=0.45,
                              cost_floor_mult=dp.NEAR_EXPIRY_COST_FLOOR_MULT,
                              max_credit_ratio=dp.NEAR_EXPIRY_MAX_CREDIT_RATIO,
                              enforce_reachability=False)
    assert not out["ok"], "an ATM 0-DTE spread must be refused"
    assert "credit_ratio_too_high" in out["reason"]
    assert "at/near the money" in out["reason"]

    # Same short strike, 3-strike wing -> ratio 0.167, lawful.
    nodes3 = [_node(77600, 88.66, -0.50), _node(77300, 38.35, -0.15)]
    ok = build_credit_spread(chain_nodes=nodes3, direction="bullish", width_points=300,
                             short_delta=0.50, lot_size=20, tp_frac=0.45,
                             cost_floor_mult=dp.NEAR_EXPIRY_COST_FLOOR_MULT,
                             max_credit_ratio=dp.NEAR_EXPIRY_MAX_CREDIT_RATIO,
                             enforce_reachability=False)
    assert ok["ok"], ok.get("reason")
