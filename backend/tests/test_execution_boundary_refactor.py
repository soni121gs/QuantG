import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class AsyncCursor:
    def __init__(self, rows=None):
        self.rows = rows or []

    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    async def to_list(self, *args, **kwargs):
        return self.rows


def result(modified=0, deleted=0):
    return SimpleNamespace(modified_count=modified, deleted_count=deleted, matched_count=modified)


@pytest.mark.anyio
async def test_multiple_users_and_strategies_have_distinct_duplicate_keys():
    import server

    key_a = server._strategy_side_key("user-a", "strat-1", "NSE_FO|123", "LONG")
    key_b = server._strategy_side_key("user-b", "strat-1", "NSE_FO|123", "LONG")
    key_c = server._strategy_side_key("user-a", "strat-2", "NSE_FO|123", "LONG")
    key_d = server._strategy_side_key("user-a", "strat-1", "NSE_FO|123", "SHORT")

    assert len({key_a, key_b, key_c, key_d}) == 4


@pytest.mark.anyio
async def test_same_user_multiple_strategies_same_instrument_allowed_to_reserve():
    import server

    mock_db = MagicMock()
    mock_db.strategy_positions.find_one = AsyncMock(return_value=None)
    mock_db.strategy_positions.insert_one = AsyncMock()
    mock_db.strategy_position_locks.insert_one = AsyncMock()
    mock_db.strategies.find_one = AsyncMock(return_value={
        "visual_config": {"risk": {"stop_loss_pct": 5, "take_profit_pct": 10}}
    })

    with patch("server.db", mock_db):
        first = await server._reserve_strategy_position(
            user_id="user-1",
            strategy_id="strat-a",
            instrument_key="NSE_FO|123",
            trading_symbol="NIFTY26060524900CE",
            exchange="NFO",
            instrument_token="NSE_FO|123",
            quantity=65,
            entry_price=100,
            position_side="LONG",
            source="strategy:strat-a",
        )
        second = await server._reserve_strategy_position(
            user_id="user-1",
            strategy_id="strat-b",
            instrument_key="NSE_FO|123",
            trading_symbol="NIFTY26060524900CE",
            exchange="NFO",
            instrument_token="NSE_FO|123",
            quantity=65,
            entry_price=100,
            position_side="LONG",
            source="strategy:strat-b",
        )

    assert first["active_strategy_instrument_side_key"] != second["active_strategy_instrument_side_key"]
    assert mock_db.strategy_positions.insert_one.await_count == 2


@pytest.mark.anyio
async def test_duplicate_same_strategy_same_instrument_side_blocked():
    import server

    mock_db = MagicMock()
    mock_db.strategy_positions.find_one = AsyncMock(return_value={"id": "pos-1", "status": "OPEN"})

    with patch("server.db", mock_db), pytest.raises(HTTPException) as exc:
        await server._reserve_strategy_position(
            user_id="user-1",
            strategy_id="strat-a",
            instrument_key="NSE_FO|123",
            trading_symbol="NIFTY26060524900CE",
            exchange="NFO",
            instrument_token="NSE_FO|123",
            quantity=65,
            entry_price=100,
            position_side="LONG",
            source="strategy:strat-a",
        )

    assert exc.value.status_code == 409
    assert "same strategy, instrument and side" in exc.value.detail


@pytest.mark.anyio
async def test_skipped_signal_persists_to_skipped_collection_not_orders():
    import server

    mock_db = MagicMock()
    mock_db.skipped_signals.find_one_and_update = AsyncMock(return_value={
        "id": "skip-1",
        "status": "SKIPPED_SIGNAL",
        "execution_status": "SKIPPED_SIGNAL",
        "reason_code": "PRICE_UNAVAILABLE",
        "mode": "paper",
    })
    mock_db.strategies.update_one = AsyncMock()
    mock_db.orders.insert_one = AsyncMock()
    intent = MagicMock()
    intent.instrument = MagicMock(
        tradingsymbol="NIFTY26060524900CE",
        exchange="NFO",
        instrument_token="NSE_FO|123",
        broker="upstox",
        asset_class="OPTION_LONG",
    )
    intent.quantity = 65
    intent.intent = "OPEN_LONG"
    intent.model_dump.return_value = {"intent": "OPEN_LONG", "quantity": 65}

    with patch("server.db", mock_db):
        doc = await server._persist_paper_skipped_order(
            user_id="user-1",
            intent=intent,
            order_type="MARKET",
            product="NRML",
            price=None,
            source="strategy:strat-a",
            strategy_id="strat-a",
            option_contract={"instrument_key": "NSE_FO|123"},
            resolution={"lots": 1, "lot_size": 65},
            idempotency_key="idem-1",
            signal_id="sig-1",
            market_snapshot={"source": "upstox-cache", "ltp": None},
            reason="No fresh quote",
            reason_code="PRICE_UNAVAILABLE",
        )

    assert doc["status"] == "SKIPPED_SIGNAL"
    mock_db.orders.insert_one.assert_not_called()


@pytest.mark.anyio
async def test_paper_execution_router_never_calls_live_broker_bridge():
    from core.execution_router import ExecutionRouter

    db = MagicMock()
    db.orders.insert_one = AsyncMock()
    db.fills.insert_one = AsyncMock()
    ledger = MagicMock()
    ledger.process_fill = AsyncMock()

    router = ExecutionRouter(db, ledger)
    router.paper_adapter.wallet.debit = AsyncMock(return_value=True)

    with patch("core.execution_router.bridge_submit_order", new_callable=AsyncMock) as live_submit:
        order = await router.route_intent("user-1", {
            "strategy_id": "strat-a",
            "symbol": "NIFTY",
            "target_symbol": "NIFTY26060524900CE",
            "side": "BUY",
            "qty": 65,
            "requested_price": 100.0,
            "exchange": "NFO",
            "segment": "NSE_FO",
            "mode": "paper",
            "idempotency_key": "idem-1",
            "stop_loss": 95.0,
            "take_profit": 110.0,
        })

    assert order["mode"] == "paper"
    live_submit.assert_not_called()


@pytest.mark.anyio
async def test_daily_paper_lifecycle_resets_today_and_marks_stale_positions():
    import server

    mock_db = MagicMock()
    mock_db.strategy_positions.update_many = AsyncMock(return_value=result(modified=2))
    mock_db.positions.update_many = AsyncMock(return_value=result(modified=1))
    mock_db.orders.update_many = AsyncMock(return_value=result(modified=5))
    mock_db.strategies.update_many = AsyncMock(return_value=result(modified=3))
    mock_db.paper_session_state.update_one = AsyncMock()

    with patch("server.db", mock_db):
        summary = await server._daily_paper_lifecycle_for_user("user-1")

    assert summary == {
        "stale_strategy_positions": 2,
        "stale_positions": 1,
        "history_orders_marked": 5,
        "strategies_reset": 3,
    }
    stale_update = mock_db.strategy_positions.update_many.await_args.args[1]
    assert stale_update["$set"]["status"] == "STALE_NEEDS_REVIEW"
    assert stale_update["$unset"]["active_strategy_instrument_side_key"] == ""


def test_sl_tp_prices_come_from_strategy_config_not_global_defaults():
    import server

    no_config = server._position_risk_prices({
        "average_buy_price": 100.0,
        "position_side": "LONG",
        "tp_sl_tsl_config": {"trailing_sl_enabled": False},
    })
    configured = server._position_risk_prices({
        "average_buy_price": 100.0,
        "position_side": "LONG",
        "tp_sl_tsl_config": {"stop_loss_pct": 5, "take_profit_pct": 12, "trailing_sl_enabled": False},
    })

    assert no_config["stop_loss"] is None
    assert no_config["take_profit"] is None
    assert configured["stop_loss"] == 95.0
    assert configured["take_profit"] == 112.0
