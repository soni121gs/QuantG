"""Tests for RES-2 live entry gate (core/entry_gate.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.entry_gate import seller_entry_gate


def _flat_closes(n=25, start=100.0, step=0.0002):
    c = [start]
    for _ in range(n - 1):
        c.append(round(c[-1] * (1 + step), 4))
    return c


def test_gate_allows_rich_range():
    # near-flat closes → tiny RV; IV 15 → rich; regime RANGE → allow.
    g = seller_entry_gate(regime="RANGE", iv=15.0, daily_closes=_flat_closes())
    assert g["allow"] is True and "passed" in g["reason"]


def test_gate_blocks_trend_down_even_when_rich():
    # This is today's failure: premium rich but selling into a downtrend.
    g = seller_entry_gate(regime="TREND_DOWN", iv=15.0, daily_closes=_flat_closes())
    assert g["allow"] is False and "not sell-safe" in g["reason"]


def test_gate_blocks_thin_edge():
    wild = [100, 106, 94, 107, 93, 108, 92, 109, 91, 110, 90] * 3  # high RV
    g = seller_entry_gate(regime="RANGE", iv=11.0, daily_closes=wild)
    assert g["allow"] is False and "RES2_GATE" in g["reason"]


def test_gate_fail_open_on_missing_iv():
    g = seller_entry_gate(regime="RANGE", iv=None, daily_closes=_flat_closes())
    assert g["allow"] is True and "fail-open" in g["reason"]


def test_gate_fail_open_on_missing_history():
    g = seller_entry_gate(regime="RANGE", iv=15.0, daily_closes=[])
    assert g["allow"] is True and "fail-open" in g["reason"]


def test_gate_allows_unknown_regime_when_rich():
    # regime detection gap → don't over-block; rich premium still allowed.
    g = seller_entry_gate(regime="UNKNOWN", iv=15.0, daily_closes=_flat_closes())
    assert g["allow"] is True


def test_gate_min_edge_configurable():
    # a modest edge passes at min_edge 0 but fails at a strict min_edge.
    closes = [100, 100.5, 100.0, 100.6, 100.1, 100.7, 100.2] * 4  # some RV
    lax = seller_entry_gate(regime="RANGE", iv=13.0, daily_closes=closes, min_edge=0.0)
    strict = seller_entry_gate(regime="RANGE", iv=13.0, daily_closes=closes, min_edge=99.0)
    assert lax["allow"] is True and strict["allow"] is False


def test_gate_can_block_on_iv_surface_richness_when_configured():
    surface = {"richness": {"available": True, "zscore": -0.5}}
    g = seller_entry_gate(
        regime="RANGE",
        iv=15.0,
        daily_closes=_flat_closes(),
        iv_surface=surface,
        min_surface_z=1.0,
    )
    assert g["allow"] is False
    assert "IV surface not rich enough" in g["reason"]
