from unittest.mock import AsyncMock, MagicMock, patch
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.mark.anyio
async def test_paper_entry_without_price_is_skipped_not_failed():
    from server import _place_order_core

    mock_db = MagicMock()
    mock_db.strategies.find_one = AsyncMock(return_value={"id": "1234", "mode": "paper", "status": "live"})
    mock_db.strategy_positions.find_one = AsyncMock(return_value=None)
    mock_db.orders.find_one = AsyncMock(return_value=None)
    mock_db.orders.insert_one = AsyncMock()
    mock_db.skipped_signals.find_one_and_update = AsyncMock(return_value={
        "id": "skip-1",
        "status": "SKIPPED_SIGNAL",
        "execution_status": "SKIPPED_SIGNAL",
        "skip_reason": "No valid Upstox websocket or REST LTP is available.",
        "reason_code": "PRICE_UNAVAILABLE",
        "mode": "paper",
        "filled_qty": 0,
        "count": 1,
    })
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
         patch.dict(os.environ, {"CORE_ENGINE_ENABLED": "false", "CORE_ENGINE_PAPER_ENABLED": "false"}), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": True, "default_product": "MIS"}), \
         patch("server._is_order_market_open", return_value=True), \
         patch("server._market_session_for_instrument", return_value={"segment": "NSE_EQ", "open": True, "status": "OPEN", "reason": "test open"}), \
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

    assert result["status"] == "SKIPPED_SIGNAL"
    assert result["mode"] == "paper"
    assert result["filled_qty"] == 0
    trade_guard.assert_not_called()
    reserve.assert_not_called()
    submit_live.assert_not_called()
    mock_db.orders.insert_one.assert_not_called()
    mock_db.skipped_signals.find_one_and_update.assert_awaited()


@pytest.mark.anyio
async def test_orders_api_handler_returns_skipped_for_paper_price_unavailable():
    import server

    mock_db = MagicMock()
    mock_db.strategies.find_one = AsyncMock(return_value={"id": "1234", "mode": "paper", "status": "live"})
    mock_db.strategy_positions.find_one = AsyncMock(return_value=None)
    mock_db.orders.find_one = AsyncMock(return_value=None)
    mock_db.orders.insert_one = AsyncMock()
    mock_db.skipped_signals.find_one_and_update = AsyncMock(return_value={
        "id": "skip-1",
        "status": "SKIPPED_SIGNAL",
        "execution_status": "SKIPPED_SIGNAL",
        "skip_reason": "No valid Upstox websocket or REST LTP is available.",
        "reason_code": "PRICE_UNAVAILABLE",
        "mode": "paper",
        "filled_qty": 0,
    })
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
         patch.dict(os.environ, {"CORE_ENGINE_ENABLED": "false", "CORE_ENGINE_PAPER_ENABLED": "false"}), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": True, "default_product": "MIS"}), \
         patch("server._is_order_market_open", return_value=True), \
         patch("server._market_session_for_instrument", return_value={"segment": "NSE_EQ", "open": True, "status": "OPEN", "reason": "test open"}), \
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

    assert body["status"] == "SKIPPED_SIGNAL"
    submit_live.assert_not_called()
    mock_db.orders.insert_one.assert_not_called()


@pytest.mark.anyio
async def test_core_paper_option_order_converts_lots_to_shares_and_routes():
    import server

    mock_db = MagicMock()
    mock_db.strategies.find_one = AsyncMock(return_value={"id": "nifty-strategy", "mode": "paper", "status": "live"})
    mock_db.orders.find_one = AsyncMock(return_value=None)
    mock_db.signals.find_one = AsyncMock(return_value=None)

    option_contract = {
        "tradingsymbol": "NIFTY26060524900CE",
        "exchange": "NFO",
        "instrument_token": "PAPER_NIFTY_CE_24900",
        "instrument_key": "PAPER_NIFTY_CE_24900",
        "lot_size": 65,
        "underlying": "NIFTY",
        "option_type": "CE",
        "ltp": 125.0,
        "simulated": True,
        "source": "PAPER_SIMULATED_CONTRACT",
    }
    captured = {}

    async def fake_evaluate_order(self, **kwargs):
        captured["risk_kwargs"] = kwargs
        return {"ok": True, "status": "APPROVED", "reason": "ok", "quantity": kwargs["requested_qty"]}

    async def fake_route_intent(self, user_id, intent_doc):
        captured["route_user_id"] = user_id
        captured["intent_doc"] = intent_doc
        return {
            "id": "paper-order-1",
            "status": "FILLED",
            "mode": "paper",
            "qty": intent_doc["qty"],
            "requested_price": intent_doc["requested_price"],
        }

    with patch("server.db", mock_db), \
         patch.dict(os.environ, {"CORE_ENGINE_ENABLED": "true", "CORE_ENGINE_PAPER_ENABLED": "true"}), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": True, "allow_simulated_prices": True}), \
         patch("core.risk_manager.RiskManager.evaluate_order", new=fake_evaluate_order), \
         patch("core.execution_router.ExecutionRouter.route_intent", new=fake_route_intent):
        result = await server._place_order_core(
            user_id="user-123",
            symbol="NIFTY",
            side="BUY",
            qty=1,
            source="strategy:nifty-strategy",
            option_contract=option_contract,
            signal_id="signal-1",
        )

    assert result["status"] == "FILLED"
    assert captured["risk_kwargs"]["requested_qty"] == 65
    assert captured["risk_kwargs"]["lot_size"] == 65
    assert captured["risk_kwargs"]["price"] == 125.0
    assert captured["intent_doc"]["qty"] == 65
    assert captured["intent_doc"]["target_symbol"] == "NIFTY26060524900CE"
    assert captured["route_user_id"] == "user-123"


@pytest.mark.anyio
async def test_recover_paper_contract_resolution_halts_is_user_and_paper_scoped():
    import server

    mock_db = MagicMock()
    recovered_rows = [
        {"id": "paper-1", "name": "NIFTY Paper", "status": "live"},
    ]
    mock_find = MagicMock()
    mock_find.to_list = AsyncMock(return_value=recovered_rows)
    mock_db.strategies.find.return_value = mock_find
    mock_db.strategies.update_many = AsyncMock(return_value=MagicMock(matched_count=1, modified_count=1))

    with patch("server.db", mock_db):
        result = await server._recover_paper_contract_resolution_halts_for_user("user-123")

    query = mock_db.strategies.update_many.await_args.args[0]
    update = mock_db.strategies.update_many.await_args.args[1]
    assert result["matched"] == 1
    assert query["user_id"] == "user-123"
    assert query["mode"] == "paper"
    assert update["$set"]["halted"] is False
    assert update["$set"]["is_halted"] is False
    assert "halt_reason" in update["$unset"]


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
        "status": "PAPER_ORDER_CREATED",
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
    assert position_doc["source_order_id"] == "order-1"
