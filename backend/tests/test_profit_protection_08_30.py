from types import SimpleNamespace

import pytest

from core.dynamic_exit import (
    evaluate_spread_exit,
    green_profit_protection_exit,
    green_profit_protection_levels,
)
from core.hermes_diagnostics.probes_execution import profit_giveback


def _credit_pos(**overrides):
    pos = {
        "id": "pos-1",
        "strategy_id": "range-seller",
        "structure": "credit_spread",
        "target_symbol": "NIFTY 24000/23800 PE CREDIT",
        "net_credit": 40.0,
        "quantity": 65,
        "open_quantity": 65,
        "lot_size": 65,
        "lots": 1,
        "spread_tp_value": 30.0,
        "spread_sl_value": 64.0,
        "legs": [
            {"entry_price": 55.0, "premium": 55.0},
            {"entry_price": 15.0, "premium": 15.0},
        ],
    }
    pos.update(overrides)
    return pos


def test_green_profit_protection_arms_and_exits_before_full_stop():
    pos = _credit_pos()

    levels = green_profit_protection_levels(pos, peak_pnl=500.0)
    assert levels["armed"] is True
    assert levels["lock_level"] and levels["lock_level"] > 0

    assert green_profit_protection_exit(
        position=pos, current_pnl=-25.0, peak_pnl=500.0
    ) == "profit-protect"


def test_credit_spread_exit_protects_green_peak_before_hard_stop():
    pos = _credit_pos()

    reason = evaluate_spread_exit(
        position=pos,
        current_value=65.0,
        current_pnl=-1625.0,
        peak_pnl=650.0,
    )

    assert reason == "profit-protect"


def test_unarmed_small_green_peak_still_uses_hard_stop():
    pos = _credit_pos()

    reason = evaluate_spread_exit(
        position=pos,
        current_value=65.0,
        current_pnl=-1625.0,
        peak_pnl=40.0,
    )

    assert reason == "spread-sl"


@pytest.mark.asyncio
async def test_profit_giveback_probe_flags_green_then_red_day():
    ctx = SimpleNamespace(
        date_str="2026-08-30",
        closed_today=[
            {
                "id": "p1",
                "strategy_id": "s1",
                "target_symbol": "NIFTY A",
                "exit_reason": "spread-sl",
                "realized_pnl": -100.0,
                "peak_pnl": 400.0,
            },
            {
                "id": "p2",
                "strategy_id": "s1",
                "target_symbol": "NIFTY B",
                "exit_reason": "spread-no-progress",
                "realized_pnl": -50.0,
                "peak_pnl": 350.0,
            },
            {
                "id": "p3",
                "strategy_id": "s2",
                "target_symbol": "SENSEX A",
                "exit_reason": "expiry-settlement",
                "realized_pnl": -200.0,
                "peak_pnl": 500.0,
            },
        ],
    )

    out = await profit_giveback(ctx)

    assert len(out) == 1
    finding = out[0]
    assert finding.probe_id == "exec.profit_giveback"
    assert finding.evidence["green_then_loss"] == 3
    assert finding.evidence["loss_after_peak"] == 1600.0
