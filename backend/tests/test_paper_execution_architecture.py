from unittest.mock import AsyncMock, MagicMock, patch
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.mark.anyio
async def test_paper_entry_without_price_is_skipped_not_failed():
    from server import _place_order_core

    mock_db = MagicMock()
    mock_db.strategies.find_one = AsyncMock(return_value={"id": "1234", "mode": "paper"})
    mock_db.orders.find_one = AsyncMock(return_value=None)
    mock_db.orders.insert_one = AsyncMock()
    mock_db.order_events.insert_one = AsyncMock()

    mock_instr = MagicMock()
    mock_instr.exchange = "NSE"
    mock_instr.tradingsymbol = "RELIANCE"
    mock_instr.segment = "EQUITY"
    mock_instr.asset_class = "DIRECT"
    mock_instr.instrument_token = "NSE_EQ|INE002A01018"
    mock_instr.broker = "upstox"
    mock_instr.model_dump.return_value = {
        "exchange": "NSE",
        "tradingsymbol": "RELIANCE",
        "segment": "EQUITY",
        "asset_class": "DIRECT",
        "instrument_token": "NSE_EQ|INE002A01018",
        "broker": "upstox",
    }

    mock_intent = MagicMock()
    mock_intent.instrument = mock_instr
    mock_intent.intent = "OPEN_LONG"
    mock_intent.quantity = 10
    mock_intent.stop_loss = None
    mock_intent.take_profit = None
    mock_intent.model_dump.return_value = {
        "intent": "OPEN_LONG",
        "quantity": 10,
        "stop_loss": None,
        "take_profit": None,
    }

    snapshot = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "instrument_key": "NSE_EQ|INE002A01018",
        "ltp": None,
        "timestamp": None,
        "received_at": None,
        "source": "unavailable",
        "feed": "unavailable",
    }

    with patch("server.db", mock_db), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": True, "default_product": "MIS"}), \
         patch("server._is_order_market_open", return_value=True), \
         patch("server._build_order_intent", new_callable=AsyncMock, return_value={"intent": mock_intent, "lot_size": 1, "lots": 1}), \
         patch("server._resolve_order_fill_hint", new_callable=AsyncMock, return_value=0.0), \
         patch("server._market_snapshot_for_intent", new_callable=AsyncMock, return_value=snapshot), \
         patch("server._check_trade_count_guard", new_callable=AsyncMock) as trade_guard, \
         patch("server._reserve_strategy_position", new_callable=AsyncMock) as reserve, \
         patch("server._submit_order_intent", new_callable=AsyncMock) as submit_live:
        result = await _place_order_core(
            user_id="user-123",
            symbol="RELIANCE",
            side="BUY",
            qty=10,
            source="strategy:1234",
            idempotency_key="sig:test-skip",
            signal_id="sig-test-skip",
        )

    assert result["status"] == "SKIPPED"
    assert result["status_message"] == "SKIPPED: price unavailable"
    assert result["mode"] == "paper"
    assert result["filled_qty"] == 0
    trade_guard.assert_not_called()
    reserve.assert_not_called()
    submit_live.assert_not_called()
    inserted_doc = mock_db.orders.insert_one.await_args.args[0]
    assert inserted_doc["status"] == "SKIPPED"
    assert inserted_doc["broker"] == "paper"
    assert inserted_doc["paper_skip_trace"]["signal_id"] == "sig-test-skip"


@pytest.mark.anyio
async def test_orders_api_handler_returns_skipped_for_paper_price_unavailable():
    import server

    mock_db = MagicMock()
    mock_db.strategies.find_one = AsyncMock(return_value={"id": "1234", "mode": "paper"})
    mock_db.orders.find_one = AsyncMock(return_value=None)
    mock_db.orders.insert_one = AsyncMock()
    mock_db.order_events.insert_one = AsyncMock()

    mock_instr = MagicMock()
    mock_instr.exchange = "NSE"
    mock_instr.tradingsymbol = "RELIANCE"
    mock_instr.segment = "EQUITY"
    mock_instr.asset_class = "DIRECT"
    mock_instr.instrument_token = "NSE_EQ|INE002A01018"
    mock_instr.broker = "upstox"
    mock_instr.model_dump.return_value = {
        "exchange": "NSE",
        "tradingsymbol": "RELIANCE",
        "segment": "EQUITY",
        "asset_class": "DIRECT",
        "instrument_token": "NSE_EQ|INE002A01018",
        "broker": "upstox",
    }

    mock_intent = MagicMock()
    mock_intent.instrument = mock_instr
    mock_intent.intent = "OPEN_LONG"
    mock_intent.quantity = 1
    mock_intent.stop_loss = None
    mock_intent.take_profit = None
    mock_intent.model_dump.return_value = {"intent": "OPEN_LONG", "quantity": 1}

    with patch("server.db", mock_db), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": True, "default_product": "MIS"}), \
         patch("server._is_order_market_open", return_value=True), \
         patch("server._build_order_intent", new_callable=AsyncMock, return_value={"intent": mock_intent, "lot_size": 1, "lots": 1}), \
         patch("server._resolve_order_fill_hint", new_callable=AsyncMock, return_value=0.0), \
         patch("server._market_snapshot_for_intent", new_callable=AsyncMock, return_value={
             "symbol": "RELIANCE",
             "exchange": "NSE",
             "instrument_key": "NSE_EQ|INE002A01018",
             "ltp": None,
             "timestamp": None,
             "received_at": None,
             "source": "unavailable",
             "feed": "unavailable",
         }), \
         patch("position_reconciler.create_manual_recovery_strategy_if_missing", new_callable=AsyncMock), \
         patch("server._submit_order_intent", new_callable=AsyncMock) as submit_live:
        body = await server.place_order(
            server.OrderReq(
                symbol="RELIANCE",
                side="BUY",
                qty=1,
                order_type="MARKET",
                exchange="NSE",
                idempotency_key="api-skip",
            ),
            user={"id": "user-123", "email": "paper@example.com"},
        )

    assert body["status"] == "SKIPPED"
    assert body["status_message"] == "SKIPPED: price unavailable"
    submit_live.assert_not_called()


@pytest.mark.anyio
async def test_paper_position_records_are_mode_scoped():
    from server import _apply_paper_fill_to_position

    mock_db = MagicMock()
    mock_db.orders.find_one_and_update = AsyncMock(return_value={
        "id": "order-1",
        "user_id": "user-123",
        "mode": "paper",
        "symbol": "RELIANCE",
        "side": "BUY",
        "qty": 5,
        "asset_type": "equity",
        "exchange": "NSE",
        "strategy_id": "1234",
        "order_intent": {"intent": "OPEN_LONG"},
    })
    mock_db.positions.find_one = AsyncMock(return_value=None)
    mock_db.positions.insert_one = AsyncMock()
    mock_db.trade_fills.insert_one = AsyncMock()
    mock_db.orders.find_one = AsyncMock(return_value=None)
    mock_db.order_events.insert_one = AsyncMock()

    with patch("server.db", mock_db):
        await _apply_paper_fill_to_position(
            {"id": "order-1", "user_id": "user-123", "mode": "paper"},
            100.0,
        )

    position_doc = mock_db.positions.insert_one.await_args.args[0]
    assert position_doc["mode"] == "paper"
    assert position_doc["broker"] == "paper"
