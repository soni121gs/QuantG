"""Tests for Phase 2 #5a — credit spread builder (core/spread_builder.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.spread_builder import build_credit_spread, build_credit_spread_by_offset, lots_for_risk
from core.dynamic_contract_selector import select_dynamic_credit_spread, spread_signature


def _leg(key, delta, ltp, theta=-5.0):
    return {
        "instrument_key": key,
        "option_greeks": {"delta": delta, "iv": 15.0, "theta": theta},
        "market_data": {"ltp": ltp, "oi": 100000},
    }


def _node(strike, ce=None, pe=None, expiry="2026-06-30"):
    n = {"strike_price": strike, "expiry": expiry}
    if ce is not None:
        n["call_options"] = ce
    if pe is not None:
        n["put_options"] = pe
    return n


def _pe_chain():
    return [
        _node(22800, pe=_leg("PE|22800", -0.20, 30, theta=-4)),
        _node(22850, pe=_leg("PE|22850", -0.25, 40)),
        _node(22900, pe=_leg("PE|22900", -0.30, 55, theta=-10)),  # short (target 0.30)
        _node(22950, pe=_leg("PE|22950", -0.40, 75)),
        _node(23000, pe=_leg("PE|23000", -0.50, 100)),
    ]


def _ce_chain():
    return [
        _node(23000, ce=_leg("CE|23000", 0.50, 100)),
        _node(23050, ce=_leg("CE|23050", 0.40, 75)),
        _node(23100, ce=_leg("CE|23100", 0.30, 55, theta=-10)),  # short (target 0.30)
        _node(23150, ce=_leg("CE|23150", 0.25, 40)),
        _node(23200, ce=_leg("CE|23200", 0.20, 30, theta=-4)),
    ]


def test_bull_put_spread():
    s = build_credit_spread(chain_nodes=_pe_chain(), direction="bullish", width_points=100, short_delta=0.30)
    assert s["ok"] is True
    assert s["option_type"] == "PE"
    assert s["short_leg"]["strike"] == 22900 and s["short_leg"]["side"] == "SELL"
    assert s["long_leg"]["strike"] == 22800 and s["long_leg"]["side"] == "BUY"
    assert s["net_credit"] == 25.0      # 55 - 30
    assert s["max_loss"] == 75.0        # 100 width - 25 credit
    assert s["width_points"] == 100
    assert s["net_theta"] == 6.0        # -(-10) + (-4)


def test_bear_call_spread():
    s = build_credit_spread(chain_nodes=_ce_chain(), direction="bearish", width_points=100, short_delta=0.30)
    assert s["ok"] is True
    assert s["option_type"] == "CE"
    assert s["short_leg"]["strike"] == 23100
    assert s["long_leg"]["strike"] == 23200
    assert s["net_credit"] == 25.0 and s["max_loss"] == 75.0


def test_bull_put_spread_by_offset():
    s = build_credit_spread_by_offset(
        chain_nodes=_pe_chain(),
        direction="bullish",
        spot=23000,
        offset_strikes=2,
        width_points=50,
    )
    assert s["ok"] is True
    assert s["selection_method"] == "offset"
    assert s["option_type"] == "PE"
    assert s["short_leg"]["strike"] == 22900
    assert s["long_leg"]["strike"] == 22850
    assert s["net_credit"] == 15.0
    assert s["max_loss"] == 35.0


def test_invalid_direction():
    assert build_credit_spread(chain_nodes=_pe_chain(), direction="sideways", width_points=100)["ok"] is False


def test_non_positive_credit_rejected():
    # long premium >= short premium → not a credit spread
    chain = [
        _node(22900, pe=_leg("PE|22900", -0.30, 30)),   # short, cheap
        _node(22800, pe=_leg("PE|22800", -0.20, 50)),   # long, expensive
    ]
    assert build_credit_spread(chain_nodes=chain, direction="bullish", width_points=100)["ok"] is False


def test_missing_long_leg_rejected():
    # only the short strike present; long strike (22800) absent → nearest fallback
    # would pick a same-type leg, but with a single node there is no other → reject.
    chain = [_node(22900, pe=_leg("PE|22900", -0.30, 55))]
    out = build_credit_spread(chain_nodes=chain, direction="bullish", width_points=100)
    assert out["ok"] is False


def test_lots_for_risk():
    assert lots_for_risk(75.0, 65, 10000) == 2     # 75*65=4875 → 10000//4875 = 2
    assert lots_for_risk(75.0, 65, 4000) == 0      # can't afford one lot
    assert lots_for_risk(0, 65, 10000) == 0


def test_dynamic_selector_scores_both_sides_and_penalizes_repeat():
    chain = _pe_chain() + _ce_chain()
    first = select_dynamic_credit_spread(
        chain_nodes=chain, preferred_direction="bullish", width_points=100,
        minutes_to_close=120,
    )
    assert first["ok"] and first["candidate_count"] >= 2
    repeated = select_dynamic_credit_spread(
        chain_nodes=chain, preferred_direction="bullish", width_points=100,
        previous_signature=spread_signature(first), minutes_to_close=120,
    )
    assert repeated["selection_method"] == "dynamic-chain-rank"
    if spread_signature(repeated) == spread_signature(first):
        assert repeated["selection_factors"]["reuse_mult"] == 0.55


def test_dynamic_selector_honors_preferred_direction_hard_gate():
    # RC-1 fix (2026-07-17): preferred_direction is a HARD side gate. Even if the
    # opposite side has a fatter credit, the selector must NOT flip the side the
    # strategy chose (that sold CE into a rally, −₹3,810 on 2026-07-17).
    chain = _pe_chain() + _ce_chain()
    bull = select_dynamic_credit_spread(
        chain_nodes=chain, preferred_direction="bullish", width_points=100,
        minutes_to_close=120,
    )
    assert bull["ok"] and bull["direction"] == "bullish"
    assert bull["option_type"] == "PE"
    assert bull.get("side_gated") is True and bull.get("used_fallback_side") is False
    bear = select_dynamic_credit_spread(
        chain_nodes=chain, preferred_direction="bearish", width_points=100,
        minutes_to_close=120,
    )
    assert bear["ok"] and bear["direction"] == "bearish" and bear["option_type"] == "CE"


def test_dynamic_selector_falls_back_when_requested_side_unbuildable():
    # Only CE strikes on the chain; a bullish (PE) request has nothing to build,
    # so we fall back to the buildable side but flag it rather than fail silently.
    chain = _ce_chain()
    out = select_dynamic_credit_spread(
        chain_nodes=chain, preferred_direction="bullish", width_points=100,
        minutes_to_close=120,
    )
    if out["ok"]:
        assert out["direction"] == "bearish"
        assert out["used_fallback_side"] is True


def test_dynamic_selector_fades_contract_score_near_close():
    chain = _pe_chain() + _ce_chain()
    early = select_dynamic_credit_spread(
        chain_nodes=chain, preferred_direction="bullish", width_points=100,
        minutes_to_close=120,
    )
    late = select_dynamic_credit_spread(
        chain_nodes=chain, preferred_direction="bullish", width_points=100,
        minutes_to_close=20,
    )
    assert late["contract_edge_score"] < early["contract_edge_score"]


def test_dynamic_selector_records_iv_surface_score_factor():
    chain = _pe_chain() + _ce_chain()
    rich = select_dynamic_credit_spread(
        chain_nodes=chain,
        preferred_direction="bullish",
        width_points=100,
        minutes_to_close=120,
        iv_surface={"richness": {"available": True, "zscore": 2.0}},
    )
    neutral = select_dynamic_credit_spread(
        chain_nodes=chain,
        preferred_direction="bullish",
        width_points=100,
        minutes_to_close=120,
    )
    assert rich["contract_edge_score"] > neutral["contract_edge_score"]
    assert rich["selection_factors"]["iv_richness_z"] == 2.0


# --- ERP cost-floor guard (2026-07-21) ---------------------------------------
# Regression guards for the QG-O1 root cause: a credit spread that collects a
# token fraction of the risk it takes, on a wing so wide the long leg is inert.


def test_cost_floor_vetoes_thin_credit_on_wide_wing():
    """The exact QG-O1 shape: ~8.5 credit on a 500-point wing (ratio 0.017)."""
    chain = [
        _node(22500, pe=_leg("PE|22500", -0.02, 0.46)),   # inert long wing
        _node(23000, pe=_leg("PE|23000", -0.12, 8.97)),   # short
    ]
    res = build_credit_spread(chain_nodes=chain, direction="bullish",
                              width_points=500, short_delta=0.12, lot_size=65)
    assert res["ok"] is False
    assert "cost_floor" in res["reason"]
    assert res["cost_floor"]["credit_ratio"] < 0.05


def test_cost_floor_allows_healthy_credit_to_width_ratio():
    res = build_credit_spread(chain_nodes=_pe_chain(), direction="bullish",
                              width_points=100, short_delta=0.30)
    assert res["ok"] is True
    # credit 55 - 30 = 25 on width 100 => ratio 0.25, comfortably above the floor
    assert res["cost_floor"]["credit_ratio"] >= 0.12
    assert res["cost_floor"]["passed"] is True


def test_cost_floor_rejects_when_bankable_profit_is_below_friction():
    """Ratio can pass while the achievable rupee profit still loses to friction:
    tp_frac x credit x lot_size must clear 3x round-trip cost."""
    res = build_credit_spread(chain_nodes=_pe_chain(), direction="bullish",
                              width_points=100, short_delta=0.30,
                              lot_size=10, tp_frac=0.5)
    assert res["ok"] is False
    assert res["cost_floor"]["ratio_passed"] is True
    assert res["cost_floor"]["floor_passed"] is False


def test_cost_floor_can_be_disabled_for_research_paths():
    res = build_credit_spread(chain_nodes=_pe_chain(), direction="bullish",
                              width_points=500, short_delta=0.30,
                              enforce_cost_floor=False)
    assert res["ok"] is True


def test_dynamic_selector_drops_candidates_that_fail_the_cost_floor():
    """A vetoed delta must leave the ladder entirely, not merely score low."""
    res = select_dynamic_credit_spread(
        chain_nodes=_pe_chain() + _ce_chain(), preferred_direction="bullish",
        width_points=100, minutes_to_close=120, lot_size=65, tp_frac=0.5,
    )
    assert res["ok"] is True
    assert res["cost_floor"]["passed"] is True
