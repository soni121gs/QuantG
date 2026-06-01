from unittest.mock import AsyncMock, MagicMock, patch
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _mock_intent(symbol="RELIANCE", exchange="NSE", segment="NSE_EQ", token="NSE_EQ|INE002A01018"):
    instr = MagicMock()
    instr.exchange = exchange
    instr.tradingsymbol = symbol
    instr.segment = segment
    instr.asset_class = "DIRECT"
    instr.instrument_token = token
    instr.broker = "upstox"
    instr.model_dump.return_value = {
        "exchange": exchange,
        "tradingsymbol": symbol,
        "segment": segment,
        "asset_class": "DIRECT",
        "instrument_token": token,
        "broker": "upstox",
    }
    intent = MagicMock()
    intent.instrument = instr
    intent.intent = "OPEN_LONG"
    intent.quantity = 1
    intent.stop_loss = None
    intent.take_profit = None
    intent.model_dump.return_value = {"intent": "OPEN_LONG", "quantity": 1}
    return intent


def _mock_db_for_skip():
    mock_db = MagicMock()
    mock_db.strategies.find_one = AsyncMock(return_value={"id": "strat-1", "mode": "paper", "status": "live"})
    mock_db.strategy_positions.find_one = AsyncMock(return_value=None)
    mock_db.orders.find_one = AsyncMock(return_value=None)
    mock_db.orders.insert_one = AsyncMock()
    mock_db.skipped_signals.find_one_and_update = AsyncMock(return_value={
        "id": "skip-1",
        "status": "SKIPPED_SIGNAL",
        "execution_status": "SKIPPED_SIGNAL",
        "reason_code": "MARKET_CLOSED",
        "skip_reason": "market closed",
        "mode": "paper",
        "filled_qty": 0,
        "count": 1,
    })
    mock_db.order_events.insert_one = AsyncMock()
    return mock_db


@pytest.mark.anyio
async def test_after_hours_signal_is_skipped_not_failed():
    from server import _place_order_core

    mock_db = _mock_db_for_skip()
    intent = _mock_intent()

    with patch("server.db", mock_db), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": True, "default_product": "MIS", "max_trades_per_day": 200}), \
         patch("server._build_order_intent", new_callable=AsyncMock, return_value={"intent": intent, "lot_size": 1, "lots": 1}), \
         patch("server._resolve_order_fill_hint", new_callable=AsyncMock, return_value=100.0), \
         patch("server._market_snapshot_for_intent", new_callable=AsyncMock, return_value={"ltp": 100.0, "source": "upstox-rest-quote"}), \
         patch("server._market_session_for_instrument", return_value={"segment": "NSE_EQ", "open": False, "status": "CLOSED", "reason": "NSE market closed"}), \
         patch("server._check_trade_count_guard", new_callable=AsyncMock) as trade_guard, \
         patch("server._reserve_strategy_position", new_callable=AsyncMock) as reserve:
        result = await _place_order_core(
            user_id="user-1",
            symbol="RELIANCE",
            side="BUY",
            qty=1,
            source="strategy:strat-1",
            idempotency_key="sig:closed",
            signal_id="closed",
        )

    assert result["status"] == "SKIPPED_SIGNAL"
    assert result["reason_code"] == "MARKET_CLOSED"
    mock_db.orders.insert_one.assert_not_called()
    reserve.assert_not_called()
    trade_guard.assert_not_called()


def test_position_requires_filled_order():
    from server import _assert_position_source_order_doc

    with pytest.raises(RuntimeError):
        _assert_position_source_order_doc({"id": "o1", "mode": "paper", "status": "FAILED"}, expected_mode="paper")
    with pytest.raises(RuntimeError):
        _assert_position_source_order_doc({"id": "o2", "mode": "paper", "status": "SKIPPED_SIGNAL"}, expected_mode="paper")
    _assert_position_source_order_doc({"id": "o3", "mode": "paper", "status": "PAPER_FILLED"}, expected_mode="paper")


def test_market_session_single_source():
    from server import market_session_snapshot

    closed = market_session_snapshot(datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc))  # 18:00 IST
    assert closed["global_status"] in {"OPEN", "PARTIAL_OPEN", "CLOSED"}
    assert closed["segments"]["NSE_FO"]["status"] == "CLOSED"
    assert closed["segments"]["NSE_FO"]["status"] == closed["NSE_FO"]["status"]
    assert closed["segments"]["MCX_FO"]["status"] == closed["MCX_FO"]["status"]


@pytest.mark.anyio
async def test_no_order_spam_for_repeated_after_hours_signal():
    from server import _place_order_core

    mock_db = _mock_db_for_skip()
    intent = _mock_intent()

    with patch("server.db", mock_db), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": True, "default_product": "MIS", "max_trades_per_day": 200}), \
         patch("server._build_order_intent", new_callable=AsyncMock, return_value={"intent": intent, "lot_size": 1, "lots": 1}), \
         patch("server._resolve_order_fill_hint", new_callable=AsyncMock, return_value=100.0), \
         patch("server._market_snapshot_for_intent", new_callable=AsyncMock, return_value={"ltp": 100.0, "source": "upstox-rest-quote"}), \
         patch("server._market_session_for_instrument", return_value={"segment": "NSE_EQ", "open": False, "status": "CLOSED", "reason": "NSE market closed"}):
        for idx in range(100):
            await _place_order_core(
                user_id="user-1",
                symbol="RELIANCE",
                side="BUY",
                qty=1,
                source="strategy:strat-1",
                idempotency_key=f"sig:closed:{idx}",
                signal_id=f"closed-{idx}",
            )

    mock_db.orders.insert_one.assert_not_called()
    assert mock_db.skipped_signals.find_one_and_update.await_count == 100


@pytest.mark.anyio
async def test_crudeoilm_future_resolution():
    from server import _build_order_intent

    mock_db = MagicMock()
    mock_db.strategy_positions.find_one = AsyncMock(return_value=None)
    with patch("server.db", mock_db), \
         patch("server._upstox_instrument_token", return_value=None), \
         patch("server._resolve_upstox_mcx_future_contract", new_callable=AsyncMock, return_value={
             "instrument_key": "MCX_FO|566001",
             "trading_symbol": "CRUDEOILM 16 JUN 26 FUT",
         }):
        result = await _build_order_intent(
            user_id="user-1",
            symbol="CRUDEOILM",
            side="BUY",
            qty=1,
            source="manual",
            exchange="MCX",
            settings={"default_qty": 1},
        )

    instr = result["intent"].instrument
    assert instr.exchange == "MCX"
    assert instr.segment == "MCX_FO"
    assert instr.instrument_token == "MCX_FO|566001"


def test_option_segment_not_equity():
    from server import _execution_segment_for

    assert _execution_segment_for("NFO", "OPTION_LONG", "NIFTY26JUN25000CE") == "NSE_FO"
    assert _execution_segment_for("NFO", "OPTION_LONG", "BANKNIFTY26JUN52000PE") == "NSE_FO"
    assert _execution_segment_for("BFO", "OPTION_LONG", "SENSEX26JUN80000CE") == "BSE_FO"
    assert _execution_segment_for("MCX", "OPTION_LONG", "CRUDEOILM26JUN6500CE") == "MCX_FO"
