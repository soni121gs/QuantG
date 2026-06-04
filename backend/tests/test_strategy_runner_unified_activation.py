import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_runner import _contract_resolution_update, _latest_signal_price


def test_mcx_paper_contract_resolution_failure_does_not_halt():
    update = _contract_resolution_update(
        eval_set={"last_pod": "pod-1"},
        inc_set={"evaluations": 1},
        action="BUY",
        signals_count=1,
        clear_reason="CRUDEOILM contract unresolved",
        is_paper_mode=True,
        is_mcx_underlying=True,
        diagnostics={
            "resolver_stage": "mcx_option_lookup",
            "resolver_reason": "no_option_match_for_underlying_expiry_strike",
            "instrument_source": None,
            "instrument_key": "MCX_FO|future-key",
            "quote_source": "UPSTOX_LIVE",
            "quote_age_sec": 0.7,
            "subscribed_key": "MCX_FO|future-key",
            "cache_lookup_key": "MCX_FO|future-key",
            "cache_hit": True,
            "quote_timestamp": "2026-06-04T10:00:00+00:00",
            "quote_reject_reason": None,
        },
    )

    assert update["$set"]["halted"] is False
    assert update["$set"]["is_halted"] is False
    assert update["$set"]["last_skip_reason_code"] == "CONTRACT_RESOLUTION_FAILED"
    assert update["$set"]["last_error"] is None
    assert update["$set"]["last_filter_reason"] == "CRUDEOILM contract unresolved"
    assert update["$set"]["last_resolver_stage"] == "mcx_option_lookup"
    assert update["$set"]["last_resolver_reason"] == "no_option_match_for_underlying_expiry_strike"
    assert update["$set"]["last_instrument_key"] == "MCX_FO|future-key"
    assert update["$set"]["last_quote_source"] == "UPSTOX_LIVE"
    assert update["$set"]["last_quote_age_sec"] == 0.7
    assert update["$set"]["subscribed_key"] == "MCX_FO|future-key"
    assert update["$set"]["cache_lookup_key"] == "MCX_FO|future-key"
    assert update["$set"]["cache_hit"] is True
    assert update["$set"]["quote_timestamp"] == "2026-06-04T10:00:00+00:00"
    assert "halt_reason" in update["$unset"]


def test_live_contract_resolution_failure_still_halts_strategy():
    update = _contract_resolution_update(
        eval_set={},
        inc_set={"evaluations": 1},
        action="BUY",
        signals_count=1,
        clear_reason="CRUDEOILM contract unresolved",
        is_paper_mode=False,
        is_mcx_underlying=True,
    )

    assert update["$set"]["halted"] is True
    assert update["$set"]["halt_reason"] == "CONTRACT_RESOLUTION_FAILED"
    assert update["$set"]["last_error"] == "CRUDEOILM contract unresolved"


def test_runner_signal_price_prefers_contract_ltp_then_latest_candle():
    assert _latest_signal_price({}, [{"close": 101.5}], {"ltp": 34.5}) == 34.5
    assert _latest_signal_price({"price": 55.0}, [{"close": 101.5}], None) == 55.0
    assert _latest_signal_price({}, [{"close": 101.5}], None) == 101.5
