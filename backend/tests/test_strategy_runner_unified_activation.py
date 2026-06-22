import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_runner import (
    _contract_resolution_update,
    _latest_signal_price,
    _select_latest_recent_signal,
    _select_paper_measurement_signal,
)


def test_paper_contract_resolution_failure_does_not_halt():
    update = _contract_resolution_update(
        eval_set={"last_pod": "pod-1"},
        inc_set={"evaluations": 1},
        action="BUY",
        signals_count=1,
        clear_reason="NIFTY contract unresolved",
        is_paper_mode=True,
        diagnostics={
            "resolver_stage": "option_lookup",
            "resolver_reason": "no_option_match_for_underlying_expiry_strike",
            "instrument_source": None,
            "instrument_key": "NSE_FO|option-key",
            "quote_source": "UPSTOX_LIVE",
            "quote_age_sec": 0.7,
            "subscribed_key": "NSE_FO|option-key",
            "cache_lookup_key": "NSE_FO|option-key",
            "cache_hit": True,
            "quote_timestamp": "2026-06-04T10:00:00+00:00",
            "quote_reject_reason": None,
        },
    )

    assert update["$set"]["halted"] is False
    assert update["$set"]["is_halted"] is False
    assert update["$set"]["last_skip_reason_code"] == "CONTRACT_RESOLUTION_FAILED"
    assert update["$set"]["last_error"] is None
    assert update["$set"]["last_filter_reason"] == "NIFTY contract unresolved"
    assert update["$set"]["last_resolver_stage"] == "option_lookup"
    assert update["$set"]["last_resolver_reason"] == "no_option_match_for_underlying_expiry_strike"
    assert update["$set"]["last_instrument_key"] == "NSE_FO|option-key"
    assert update["$set"]["last_quote_source"] == "UPSTOX_LIVE"
    assert update["$set"]["last_quote_age_sec"] == 0.7
    assert update["$set"]["subscribed_key"] == "NSE_FO|option-key"
    assert update["$set"]["cache_lookup_key"] == "NSE_FO|option-key"
    assert update["$set"]["cache_hit"] is True
    assert update["$set"]["quote_timestamp"] == "2026-06-04T10:00:00+00:00"
    assert "halt_reason" in update["$unset"]


def test_live_contract_resolution_failure_marks_skip_without_halting_strategy():
    update = _contract_resolution_update(
        eval_set={},
        inc_set={"evaluations": 1},
        action="BUY",
        signals_count=1,
        clear_reason="NIFTY contract unresolved",
        is_paper_mode=False,
    )

    assert update["$set"]["halted"] is False
    assert update["$set"]["is_halted"] is False
    assert update["$set"]["last_skip_reason_code"] == "CONTRACT_RESOLUTION_FAILED"
    assert update["$set"]["last_error"] == "NIFTY contract unresolved"
    assert "halt_reason" in update["$unset"]


def test_runner_signal_price_prefers_contract_ltp_then_latest_candle():
    assert _latest_signal_price({}, [{"close": 101.5}], {"ltp": 34.5}) == 34.5
    assert _latest_signal_price({"price": 55.0}, [{"close": 101.5}], None) == 55.0
    assert _latest_signal_price({}, [{"close": 101.5}], None) == 101.5


def test_runner_selects_latest_recent_signal_not_stale_tail():
    data = [
        {"date": "2026-06-19 13:55", "close": 100.0},
        {"date": "2026-06-22 12:30", "close": 101.0},
        {"date": "2026-06-22 12:35", "close": 102.0},
    ]
    signals = [
        {"date": "2026-06-19 13:55", "action": "BUY"},
        {"date": "2026-06-22 12:30", "action": "SELL"},
    ]

    assert _select_latest_recent_signal(signals, data) == signals[1]


def test_runner_returns_none_when_only_historical_signals_exist():
    data = [
        {"date": "2026-06-22 12:30", "close": 101.0},
        {"date": "2026-06-22 12:35", "close": 102.0},
    ]
    signals = [{"date": "2026-06-19 13:55", "action": "BUY"}]

    assert _select_latest_recent_signal(signals, data) is None


def test_runner_paper_measurement_selects_same_session_signal():
    data = [
        {"date": "2026-06-22 12:00", "close": 100.0},
        {"date": "2026-06-22 12:05", "close": 101.0},
        {"date": "2026-06-22 12:10", "close": 102.0},
        {"date": "2026-06-22 12:15", "close": 103.0},
    ]
    signals = [{"date": "2026-06-22 12:00", "action": "BUY"}]

    assert _select_paper_measurement_signal(signals, data) == signals[0]


def test_runner_paper_measurement_ignores_prior_session_signal():
    data = [
        {"date": "2026-06-22 12:10", "close": 102.0},
        {"date": "2026-06-22 12:15", "close": 103.0},
    ]
    signals = [{"date": "2026-06-21 12:15", "action": "BUY"}]

    assert _select_paper_measurement_signal(signals, data) is None
