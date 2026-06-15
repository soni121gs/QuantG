"""Unit tests for the paper-fill slippage model (_estimate_slippage).

A simulated market order crosses the book, so the realistic per-unit cost is
~half the bid-ask spread. These tests pin the preference order:
  bid/ask  →  spread_pct  →  asset-class base bps, with a floor and a cap.
Pure function, no DB/broker.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.execution_router import _estimate_slippage  # noqa: E402
from config import PAPER_TRADING  # noqa: E402


def test_half_spread_from_bid_ask():
    # bid 100 / ask 104 → spread 4 → half = 2.0
    slip = _estimate_slippage(102.0, {"bid": 100.0, "ask": 104.0}, is_option=True)
    assert slip == 2.0


def test_alt_bid_ask_field_names():
    slip = _estimate_slippage(102.0, {"best_bid_price": 100.0, "best_ask_price": 104.0}, is_option=True)
    assert slip == 2.0


def test_spread_pct_fallback_when_no_bid_ask():
    # spread_pct 4% of 100 → 4.0 spread → half = 2.0
    slip = _estimate_slippage(100.0, {"spread_pct": 4.0}, is_option=True)
    assert slip == 2.0


def test_option_base_when_book_unknown():
    # No bid/ask, no spread_pct → option base 0.30% of 200 = 0.60
    slip = _estimate_slippage(200.0, {}, is_option=True)
    assert slip == round(200.0 * PAPER_TRADING.OPTION_SLIPPAGE_PCT / 100.0, 2)


def test_equity_base_lower_than_option_base():
    eq = _estimate_slippage(200.0, {}, is_option=False)
    opt = _estimate_slippage(200.0, {}, is_option=True)
    assert eq < opt
    # equity base is the 5 bps floor
    assert eq == round(200.0 * PAPER_TRADING.SLIPPAGE_PCT / 100.0, 2)


def test_floor_applied_for_tight_book():
    # Razor-tight book (bid 100 / ask 100) → half-spread 0, but floor kicks in.
    slip = _estimate_slippage(100.0, {"bid": 100.0, "ask": 100.0}, is_option=True)
    floor = round(100.0 * PAPER_TRADING.SLIPPAGE_PCT / 100.0, 2)
    assert slip == floor
    assert slip > 0


def test_cap_applied_for_garbage_quote():
    # bid 0.05 / ask 50 → half-spread ~25, but capped at SLIPPAGE_MAX_PCT of price.
    slip = _estimate_slippage(1.0, {"bid": 0.05, "ask": 50.0}, is_option=True)
    cap = round(1.0 * PAPER_TRADING.SLIPPAGE_MAX_PCT / 100.0, 2)
    assert slip == cap


def test_zero_price_returns_zero():
    assert _estimate_slippage(0.0, {"bid": 1.0, "ask": 2.0}, is_option=True) == 0.0


def test_inverted_book_ignored():
    # ask < bid is invalid → fall through to option base, not a negative spread.
    slip = _estimate_slippage(100.0, {"bid": 105.0, "ask": 100.0}, is_option=True)
    assert slip == round(100.0 * PAPER_TRADING.OPTION_SLIPPAGE_PCT / 100.0, 2)
