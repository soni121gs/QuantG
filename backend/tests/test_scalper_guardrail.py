"""Audit #9 — scalper category guardrail in normalize_strategy_risk.

A strategy tagged strategy_category="scalper" must never end up with swing-like
throttling limits. normalize_strategy_risk clamps (never raises) so it stays
total over legacy position data it is also called on.
"""
from core.position_lifecycle import (
    normalize_strategy_risk,
    SCALPER_MAX_COOLDOWN_MIN,
    SCALPER_MIN_TRADES_DAY,
)


def test_scalper_swing_limits_are_clamped():
    risk = normalize_strategy_risk({
        "strategy_category": "scalper",
        "cooldown_minutes": 18,   # swing-like — must clamp down
        "max_trades_day": 2,      # swing-like — must clamp up
        "stop_loss_pct": 5,
        "take_profit_pct": 8,
    })
    assert risk["cooldown_minutes"] == SCALPER_MAX_COOLDOWN_MIN
    assert risk["max_trades_day"] == SCALPER_MIN_TRADES_DAY
    assert risk["strategy_category"] == "scalper"


def test_scalper_valid_limits_pass_through():
    risk = normalize_strategy_risk({
        "strategy_category": "scalper",
        "cooldown_minutes": 1,
        "max_trades_day": 20,
        "stop_loss_pct": 5,
        "take_profit_pct": 8,
    })
    assert risk["cooldown_minutes"] == 1
    assert risk["max_trades_day"] == 20


def test_non_scalper_categories_not_clamped():
    for cat in ("swing", "intraday"):
        risk = normalize_strategy_risk({
            "strategy_category": cat,
            "cooldown_minutes": 20,
            "max_trades_day": 2,
            "stop_loss_pct": 5,
            "take_profit_pct": 8,
        })
        assert risk["cooldown_minutes"] == 20
        assert risk["max_trades_day"] == 2
        assert risk["strategy_category"] == cat


def test_no_category_is_inert_for_legacy_position_reads():
    # A position's tp_sl_tsl_config carries no category — must not be touched.
    risk = normalize_strategy_risk({
        "cooldown_minutes": 18,
        "max_trades_day": 2,
        "stop_loss_pct": 5,
        "take_profit_pct": 8,
    })
    assert risk["cooldown_minutes"] == 18
    assert risk["max_trades_day"] == 2
    assert "strategy_category" not in risk
