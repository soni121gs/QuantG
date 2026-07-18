import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from safe_exec import safe_run_strategy
from server import (
    DEAD_STRATEGY_NAMES,
    DEFAULT_OPTION_STRATEGIES,
    OPTION_ALPHA_REBUILD_NAMES,
    PAPER_FORWARD_ACTIVE_STRATEGY_NAMES,
    PAPER_FORWARD_KEEP_STRATEGY_NAMES,
    PAPER_FORWARD_ARCHIVED_STRATEGY_NAMES,
    RETAIL_LIVE_STATE_CODE,
    UPGRADED_DEFAULT_STRATEGY_CODE_BY_NAME,
)


EXPECTED_DEFAULT_NAMES = {
    "QG-O1 NIFTY Put Spread Theta Core",
    "QG-O4 SENSEX Call Spread Range Pilot",
    "QG-O11 NIFTY Regime Seller Credit Scalp",
}


def _sample_candles(count=90):
    candles = []
    price = 24000.0
    for i in range(count):
        if i < 35:
            price += 4.0
        elif i < 55:
            price += 18.0
        else:
            price -= 7.0
        candles.append({
            "date": f"2026-06-05 {9 + (15 + i * 5) // 60:02d}:{(15 + i * 5) % 60:02d}",
            "open": price - 8.0,
            "high": price + 18.0,
            "low": price - 14.0,
            "close": price,
            "volume": 1000 + i * 8,
        })
    return candles


def test_default_strategy_catalog_is_the_phase0_code_backed_keeper_set():
    names = {strategy["name"] for strategy in DEFAULT_OPTION_STRATEGIES}

    assert names == EXPECTED_DEFAULT_NAMES
    assert len(DEFAULT_OPTION_STRATEGIES) == 3
    assert all(strategy["instrument_group"] in {"NFO", "BFO"} for strategy in DEFAULT_OPTION_STRATEGIES)
    assert all(
        strategy["underlying"] in {"NIFTY", "SENSEX"}
        for strategy in DEFAULT_OPTION_STRATEGIES
    )


def test_default_strategy_templates_are_not_collapsed_to_generic_retail_code():
    generic = RETAIL_LIVE_STATE_CODE.strip()

    for strategy in DEFAULT_OPTION_STRATEGIES:
        code = (strategy.get("python_code") or "").strip()
        assert code != generic, strategy["name"]
        assert "position = \"NONE\"" in code, strategy["name"]
        if strategy.get("instrument_group") in ("NSE", "BSE"):
            # Cash equity templates do not require option-style exits or options-specific risk styles
            continue
        assert (
            "time exit" in code.lower()
            or "scalper time exit" in code.lower()
            or "max_hold_minutes" in code
            or strategy["name"].startswith("QG-O")
        ), strategy["name"]
        assert strategy.get("risk_style") in {"momentum", "breakout", "pullback", "micro_scalp", "volatile_breakout"}


def test_equity_templates_are_purged_from_phase0_seed_catalog():
    equity_names = {
        strategy["name"]
        for strategy in DEFAULT_OPTION_STRATEGIES
        if strategy.get("instrument_group") in ("NSE", "BSE")
    }

    assert not equity_names
    assert "RELIANCE Trend Rider" in DEAD_STRATEGY_NAMES
    assert "RELIANCE Trend Rider" in UPGRADED_DEFAULT_STRATEGY_CODE_BY_NAME


def test_phase0_keeps_only_rejudge_qg_rows_in_seed_catalog():
    names = {strategy["name"] for strategy in DEFAULT_OPTION_STRATEGIES}

    assert names == (OPTION_ALPHA_REBUILD_NAMES - PAPER_FORWARD_ARCHIVED_STRATEGY_NAMES)
    assert names.isdisjoint(DEAD_STRATEGY_NAMES)


def test_phase0_book_has_no_auto_active_qg_rows():
    assert PAPER_FORWARD_ACTIVE_STRATEGY_NAMES == set()
    assert {
        "QG-O1 NIFTY Put Spread Theta Core",
        "QG-O4 SENSEX Call Spread Range Pilot",
        "QG-O11 NIFTY Regime Seller Credit Scalp",
    }.issubset(PAPER_FORWARD_KEEP_STRATEGY_NAMES)
    assert PAPER_FORWARD_ARCHIVED_STRATEGY_NAMES == OPTION_ALPHA_REBUILD_NAMES - PAPER_FORWARD_KEEP_STRATEGY_NAMES
    by_name = {strategy["name"]: strategy for strategy in DEFAULT_OPTION_STRATEGIES}
    o11 = by_name["QG-O11 NIFTY Regime Seller Credit Scalp"]
    assert o11["structure"] == "credit_spread"
    assert o11["spread_width"] == 1
    assert o11["short_offset_strikes"] == 1
    assert o11["credit_tp_frac"] == 0.50
    assert o11["credit_sl_mult"] == 0.90
    assert o11["candle_interval"] == "1minute"
    # scalp pacing must survive the blanket CREDIT_SPREAD_THETA_RISK update
    assert o11["risk"]["max_trades_day"] == 3
    assert o11["risk"]["cooldown_minutes"] == 20


def test_default_strategy_templates_are_sandbox_runnable():
    candles = _sample_candles()

    for strategy in DEFAULT_OPTION_STRATEGIES:
        result = safe_run_strategy(strategy["python_code"], candles)
        assert isinstance(result, list), strategy["name"]


def test_ema_named_default_strategies_use_true_ema_formula():
    ema_names = {
        "NIFTY Momentum Buyer",
        "NIFTY Quick EMA Scalper",
    }

    for strategy in DEFAULT_OPTION_STRATEGIES:
        if strategy["name"] in ema_names:
            code = strategy["python_code"]
            assert "def ema(values, period)" in code, strategy["name"]
            assert "k = 2.0 / (period + 1)" in code, strategy["name"]
