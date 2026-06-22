import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from safe_exec import safe_run_strategy
from server import DEFAULT_OPTION_STRATEGIES, RETAIL_LIVE_STATE_CODE, UPGRADED_DEFAULT_STRATEGY_CODE_BY_NAME


EXPECTED_DEFAULT_NAMES = {
    "UPSTOX NIFTY ATM Option Momentum Buyer",
    "UPSTOX BANKNIFTY ATM Option Breakout Buyer",
    "NIFTY VWAP Trend Breakout",
    "SENSEX Swing RSI Pullback",
    "NIFTY Micro-Lot Trend Follower",
    "NIFTY HFT Quick Scalper",
    "BANKNIFTY HFT Momentum Scalper",
    "NIFTY Quick EMA Scalper",
    "BANKNIFTY Volatility Breakout",
    "UPSTOX RELIANCE Advanced Momentum Trend Rider",
    "UPSTOX SBIN Macro Short Seller",
    "UPSTOX HDFCBANK Range Mean Reversion",
    "UPSTOX ICICIBANK News & Volatility Catalyst",
    "UPSTOX TCS Defensive Swing Accumulator",
    "UPSTOX INFY VWAP Pullback Buyer",
    "UPSTOX AXISBANK Macro Trend Follower",
    "UPSTOX LT Infrastructure Momentum Rider",
    "UPSTOX BHARTIARTL Defensive Intraday Trend",
    "UPSTOX KOTAKBANK RSI Rebound Swing",
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


def test_default_strategy_catalog_is_the_reported_nine_supported_option_buyers():
    names = {strategy["name"] for strategy in DEFAULT_OPTION_STRATEGIES}

    assert names == EXPECTED_DEFAULT_NAMES
    assert len(DEFAULT_OPTION_STRATEGIES) == 19
    assert all(strategy["instrument_group"] in {"NFO", "BFO", "NSE", "BSE"} for strategy in DEFAULT_OPTION_STRATEGIES)
    assert all(
        strategy["underlying"] in {
            "NIFTY", "BANKNIFTY", "SENSEX", "RELIANCE", "TCS", "HDFCBANK",
            "ICICIBANK", "SBIN", "INFY", "AXISBANK", "LT", "BHARTIARTL", "KOTAKBANK"
        }
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
        assert "time exit" in code.lower() or "scalper time exit" in code.lower(), strategy["name"]
        assert strategy.get("risk_style") in {"momentum", "breakout", "pullback", "micro_scalp", "volatile_breakout"}


def test_equity_templates_are_in_versioned_code_migration():
    equity_names = {
        strategy["name"]
        for strategy in DEFAULT_OPTION_STRATEGIES
        if strategy.get("instrument_group") in ("NSE", "BSE")
    }

    assert equity_names
    assert equity_names.issubset(UPGRADED_DEFAULT_STRATEGY_CODE_BY_NAME)


def test_default_strategy_templates_are_sandbox_runnable():
    candles = _sample_candles()

    for strategy in DEFAULT_OPTION_STRATEGIES:
        result = safe_run_strategy(strategy["python_code"], candles)
        assert isinstance(result, list), strategy["name"]


def test_ema_named_default_strategies_use_true_ema_formula():
    ema_names = {
        "UPSTOX NIFTY ATM Option Momentum Buyer",
        "NIFTY Quick EMA Scalper",
    }

    for strategy in DEFAULT_OPTION_STRATEGIES:
        if strategy["name"] in ema_names:
            code = strategy["python_code"]
            assert "def ema(values, period)" in code, strategy["name"]
            assert "k = 2.0 / (period + 1)" in code, strategy["name"]
