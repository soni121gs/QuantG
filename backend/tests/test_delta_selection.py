"""Tests for Phase 2 #2 — delta-based strike selection (options_delta.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from options_delta import pick_delta_strike, target_delta_for_style


def _opt(delta, ltp=100.0, key="NSE_FO|1"):
    return {"instrument_key": key, "option_greeks": {"delta": delta}, "market_data": {"ltp": ltp}}


def _node(strike, ce=None, pe=None, expiry="2026-06-30"):
    n = {"strike_price": strike, "expiry": expiry}
    if ce is not None:
        n["call_options"] = ce
    if pe is not None:
        n["put_options"] = pe
    return n


def _ce_chain():
    return [
        _node(54000, ce=_opt(0.50, key="NSE_FO|A")),
        _node(54500, ce=_opt(0.35, key="NSE_FO|B")),
        _node(55000, ce=_opt(0.20, key="NSE_FO|C")),
    ]


def test_picks_closest_delta_ce():
    pick = pick_delta_strike(_ce_chain(), "CE", 0.30)
    assert pick["strike"] == 54500 and pick["opt_node"]["instrument_key"] == "NSE_FO|B"


def test_picks_atm_for_high_target():
    pick = pick_delta_strike(_ce_chain(), "CE", 0.50)
    assert pick["strike"] == 54000


def test_pe_uses_absolute_delta():
    chain = [
        _node(54000, pe=_opt(-0.50, key="NSE_FO|P1")),
        _node(53500, pe=_opt(-0.30, key="NSE_FO|P2")),
    ]
    pick = pick_delta_strike(chain, "PE", 0.30)
    assert pick["strike"] == 53500 and pick["delta"] == -0.30


def test_skips_nodes_without_delta():
    chain = [
        _node(54000, ce={"instrument_key": "NSE_FO|A", "market_data": {"ltp": 100}}),  # no greeks
        _node(54500, ce=_opt(0.40, key="NSE_FO|B")),
    ]
    pick = pick_delta_strike(chain, "CE", 0.45)
    assert pick["strike"] == 54500


def test_skips_illiquid_low_ltp():
    chain = [
        _node(54000, ce=_opt(0.30, ltp=0.1, key="NSE_FO|A")),  # matches delta but illiquid
        _node(54500, ce=_opt(0.50, ltp=120, key="NSE_FO|B")),
    ]
    pick = pick_delta_strike(chain, "CE", 0.30, min_ltp=0.5)
    assert pick["strike"] == 54500  # illiquid 0.30 strike skipped


def test_skips_node_without_instrument_key():
    chain = [_node(54000, ce={"option_greeks": {"delta": 0.30}, "market_data": {"ltp": 100}})]
    assert pick_delta_strike(chain, "CE", 0.30) is None


def test_empty_chain_returns_none():
    assert pick_delta_strike([], "CE", 0.45) is None
    assert pick_delta_strike(None, "CE", 0.45) is None


def test_target_delta_for_style():
    # Buyer targets: slightly-OTM, not ATM (ATM=0.50 is max premium + max theta).
    assert target_delta_for_style("momentum") == 0.40
    assert target_delta_for_style("micro_scalp") == 0.35
    assert target_delta_for_style("breakout") == 0.35
    assert target_delta_for_style("volatile_breakout") == 0.35
    assert target_delta_for_style(None) == 0.45  # default
    assert target_delta_for_style("nonsense") == 0.45  # default
