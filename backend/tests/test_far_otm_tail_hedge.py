"""Far-OTM tail-hedge debit put spread (2026-08-01).

The validated long-vol tail structure is a far-OTM long put. The live single-leg
path cannot BUY puts (directional-buyer / credit-seller only), so the deployable
vehicle is a bear PUT DEBIT spread whose LONG leg sits far OTM — achieved with the
EXISTING build_debit_spread by passing a LOW long_delta (no builder change). This
pins that: a low long_delta lands the long put far below spot with a cheap debit and
a convex (multi-x) payoff, while the default 0.50 keeps the legacy near-ATM behavior.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.spread_builder import build_debit_spread


def _leg(key, delta, ltp):
    return {"instrument_key": key, "option_greeks": {"delta": delta, "iv": 15.0, "theta": -3.0},
            "market_data": {"ltp": ltp, "oi": 50000}}


def _node(strike, pe):
    return {"strike_price": strike, "expiry": "2026-06-30", "put_options": pe}


def _far_otm_pe_chain():
    # spot ~23000; puts from ATM (-0.50) down to deep far-OTM (-0.08)
    return [
        _node(23000, _leg("PE|23000", -0.50, 120)),   # ATM
        _node(22800, _leg("PE|22800", -0.35, 70)),
        _node(22600, _leg("PE|22600", -0.22, 40)),
        _node(22500, _leg("PE|22500", -0.15, 28)),     # far-OTM long candidate (~2% OTM)
        _node(22400, _leg("PE|22400", -0.11, 18)),
        _node(22300, _leg("PE|22300", -0.08, 12)),     # further-OTM short candidate
    ]


def test_low_long_delta_places_long_put_far_otm():
    s = build_debit_spread(chain_nodes=_far_otm_pe_chain(), direction="bearish",
                           width_points=200, long_delta=0.15)
    assert s["ok"] is True
    assert s["option_type"] == "PE"
    # long leg sits FAR below spot (the 0.15-delta strike), not ATM
    assert s["long_leg"]["strike"] == 22500 and s["long_leg"]["side"] == "BUY"
    assert s["short_leg"]["strike"] == 22300 and s["short_leg"]["side"] == "SELL"
    # cheap debit, convex payoff (pay ~16 to make up to ~184 on a crash to the short)
    assert 0 < s["net_debit"] <= 30
    assert s["max_profit"] >= 5 * s["net_debit"]     # >=5x convexity


def test_default_delta_keeps_legacy_near_atm_behavior():
    # long_delta 0.50 must still pick the ATM long — the existing IDX debit sleeves
    # rely on this; the far-OTM behavior is OPT-IN via a low delta, not a global change.
    s = build_debit_spread(chain_nodes=_far_otm_pe_chain(), direction="bearish",
                           width_points=200, long_delta=0.50)
    assert s["ok"] is True
    assert s["long_leg"]["strike"] == 23000   # ATM, unchanged


def test_far_otm_is_much_cheaper_than_atm():
    far = build_debit_spread(chain_nodes=_far_otm_pe_chain(), direction="bearish",
                             width_points=200, long_delta=0.15)
    atm = build_debit_spread(chain_nodes=_far_otm_pe_chain(), direction="bearish",
                             width_points=200, long_delta=0.50)
    # the far-OTM hedge costs a fraction of the ATM spread (cheaper insurance)
    assert far["net_debit"] < atm["net_debit"]
