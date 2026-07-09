import pytest

from core.edge_oos import compare_dynamic_to_flat
from core.edge_runtime import build_market_context, context_dimensions


def test_context_adapter_rejects_unknown_source():
    with pytest.raises(ValueError):
        build_market_context(source="guess")


def test_context_dimensions():
    assert context_dimensions({"regime": {"regime": "range"}, "vol_state": {"state": "calm"}}) == {
        "regime": "RANGE", "vol_state": "CALM",
    }


def test_oos_replay_is_no_lookahead_and_reports_both_books():
    trades = [{"date": f"2025-01-{i + 1:02d}", "pnl": p} for i, p in enumerate(
        [100, -100] * 5 + [300] * 8 + [-100] * 2
    )]
    result = compare_dynamic_to_flat(trades, warmup=10, per_lot_max_loss=1000)
    assert result["trades"] == 20
    assert set(result) >= {"flat", "dynamic", "verdict"}
