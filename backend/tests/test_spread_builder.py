"""Tests for Phase 2 #5a — credit spread builder (core/spread_builder.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.spread_builder import build_credit_spread, lots_for_risk


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
