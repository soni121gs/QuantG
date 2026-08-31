import pytest

from core.dynamic_exit import (
    evaluate_debit_spread_exit,
    green_profit_protection_levels,
)
from core.strategy_governor import classify_strategy


def test_governor_pauses_negative_expectancy_high_wr_seller():
    row = {
        "closed": 99,
        "open": 0,
        "pnl": -6903.2,
        "avg": -69.73,
        "profit_factor": 0.751,
        "wins": 53,
        "losses": 46,
        "avg_win": 392.81,
        "worst": -1683.31,
        "green_then_loss": 40,
        "green_closed": 93,
    }

    decision = classify_strategy(row)

    assert decision["label"] == "pause"
    assert any("profit factor" in r for r in decision["reasons"])
    assert any("negative expectancy" in r for r in decision["reasons"])


def test_governor_keeps_thin_positive_sample_observe_only():
    row = {
        "closed": 12,
        "open": 2,
        "pnl": 4793.78,
        "avg": 399.48,
        "profit_factor": 1.603,
        "wins": 9,
        "losses": 3,
        "avg_win": 1415.85,
        "worst": -3714.23,
        "green_then_loss": 3,
        "green_closed": 12,
    }

    decision = classify_strategy(row)

    assert decision["label"] == "observe"
    assert decision["confidence"] == "thin_sample"


def test_governor_observes_positive_strategy_with_risk_warnings():
    row = {
        "closed": 22,
        "open": 1,
        "pnl": 14965.44,
        "avg": 680.25,
        "profit_factor": 31.182,
        "wins": 9,
        "losses": 13,
        "avg_win": 1717.92,
        "worst": -68.31,
        "green_then_loss": 13,
        "green_closed": 22,
    }

    decision = classify_strategy(row)

    assert decision["label"] == "observe"
    assert any("losers were green" in r for r in decision["reasons"])


def test_governor_marks_catastrophic_debit_spread_as_kill_candidate():
    row = {
        "closed": 2,
        "open": 0,
        "pnl": -8225.37,
        "avg": -4112.69,
        "profit_factor": None,
        "wins": 0,
        "losses": 2,
        "avg_win": None,
        "worst": -8077.26,
        "green_then_loss": 2,
        "green_closed": 2,
    }

    decision = classify_strategy(row)

    assert decision["label"] == "kill_candidate"


def test_debit_spread_payback_books_before_unreachable_formal_target():
    pos = {
        "structure": "debit_spread",
        "net_debit": 14.5,
        "average_buy_price": 14.5,
        "open_quantity": 195,
        "quantity": 195,
        "spread_tp_value": 60.88,
        "spread_sl_value": 7.25,
        "legs": [
            {"strike": 23600.0, "entry_price": 15.95},
            {"strike": 23800.0, "entry_price": 30.45},
        ],
    }

    reason = evaluate_debit_spread_exit(
        position=pos,
        current_value=25.9,
        current_pnl=2223.0,
        peak_pnl=3042.0,
    )

    assert reason == "debit-payback-tp"


def test_debit_profit_protection_profile_uses_tighter_giveback():
    pos = {
        "structure": "debit_spread",
        "net_debit": 14.5,
        "average_buy_price": 14.5,
        "open_quantity": 195,
        "quantity": 195,
        "legs": [
            {"strike": 23600.0, "entry_price": 15.95},
            {"strike": 23800.0, "entry_price": 30.45},
        ],
    }

    levels = green_profit_protection_levels(pos, peak_pnl=3042.0)

    assert levels["armed"] is True
    assert levels["lock_level"] == pytest.approx(1977.3)
