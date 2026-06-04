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
    )

    assert update["$set"]["halted"] is False
    assert update["$set"]["is_halted"] is False
    assert update["$set"]["last_skip_reason_code"] == "CONTRACT_RESOLUTION_FAILED"
    assert update["$set"]["last_error"] is None
    assert update["$set"]["last_filter_reason"] == "CRUDEOILM contract unresolved"
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
