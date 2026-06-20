import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from ltp_resolver import should_trust_ws_cache
from core.execution_router import PaperAdapter
from core.portfolio_ledger import PortfolioLedger
from core.risk_manager import RiskManager
from routes.strategies import toggle_strategy
from routes.ops import ops_enable_all_strategies


def test_should_trust_ws_cache_equity():
    # TASK-EQ-01: verify cash equity tokens now trust the cache
    assert should_trust_ws_cache("NSE_EQ|INE002A01018") is True
    assert should_trust_ws_cache("NFO|NIFTY26JUN24850CE") is True
    assert should_trust_ws_cache(None) is False


@pytest.mark.asyncio
async def test_paper_adapter_rejects_nominal_equity_fills():
    # TASK-EQ-02: verify nominal fill prices <= 0.05 are rejected for equity
    db = MagicMock()
    ledger = MagicMock()
    adapter = PaperAdapter(db, ledger)

    intent = {
        "id": "ord_1",
        "strategy_id": "strat_1",
        "symbol": "RELIANCE",
        "target_symbol": "RELIANCE",
        "side": "BUY",
        "qty": 10,
        "requested_price": 0.05,  # <= 0.05 nominal
        "asset_type": "equity",
        "exchange": "NSE",
        "segment": "NSE_EQ",
    }

    with pytest.raises(ValueError, match="Nominal fill price"):
        await adapter.execute("user_1", intent)

    # Valid price should not raise ValueError (will try to debit the wallet)
    intent["requested_price"] = 1500.0

    # Build explicit sub-mocks to avoid MagicMock awaitable issues
    paper_wallets_mock = MagicMock()
    paper_wallets_mock.find_one = AsyncMock(return_value={"user_id": "user_1", "balance": 500000.0})
    paper_wallets_mock.update_one = AsyncMock()
    db.paper_wallets = paper_wallets_mock

    paper_wallet_credits_mock = MagicMock()
    paper_wallet_credits_mock.insert_one = AsyncMock()
    db.paper_wallet_credits = paper_wallet_credits_mock

    strategy_positions_mock = MagicMock()
    strategy_positions_mock.find_one = AsyncMock(return_value=None)
    strategy_positions_mock.insert_one = AsyncMock()
    db.strategy_positions = strategy_positions_mock

    positions_mock = MagicMock()
    positions_mock.update_one = AsyncMock()
    db.positions = positions_mock

    trade_fills_mock = MagicMock()
    trade_fills_mock.insert_one = AsyncMock()
    db.trade_fills = trade_fills_mock

    orders_mock = MagicMock()
    orders_mock.insert_one = AsyncMock()
    orders_mock.update_one = AsyncMock()
    db.orders = orders_mock

    # Stub ledger.process_fill to return successful fill result
    ledger.process_fill = AsyncMock(return_value={
        "accepted": True,
        "action": "OPEN",
        "position_id": "pos_1",
        "qty_closed": 0,
        "realized_pnl": 0.0,
    })

    res = await adapter.execute("user_1", intent)
    assert res.get("status") == "FILLED"


@pytest.mark.asyncio
async def test_portfolio_ledger_override_exit_reason():
    # TASK-EQ-02: verify exit reason is overridden based on realized pnl
    db = MagicMock()
    ledger = PortfolioLedger(db)

    # Mock an open position to be closed
    strategy_positions_mock = MagicMock()
    strategy_positions_mock.find_one = AsyncMock(return_value={
        "id": "pos_123",
        "user_id": "user_1",
        "strategy_id": "strat_1",
        "symbol": "RELIANCE",
        "target_symbol": "RELIANCE",
        "mode": "paper",
        "status": "OPEN",
        "open_quantity": 10,
        "average_price": 100.0,
        "position_side": "LONG",
        "entry_charges": 2.0,
    })
    strategy_positions_mock.update_one = AsyncMock()
    db.strategy_positions = strategy_positions_mock

    positions_mock = MagicMock()
    positions_mock.delete_one = AsyncMock()
    db.positions = positions_mock

    trade_fills_mock = MagicMock()
    trade_fills_mock.insert_one = AsyncMock()
    db.trade_fills = trade_fills_mock

    trades_mock = MagicMock()
    trades_mock.insert_one = AsyncMock()
    db.trades = trades_mock

    strategies_mock = MagicMock()
    strategies_mock.update_one = AsyncMock()
    db.strategies = strategies_mock

    strategy_loss_streaks_mock = MagicMock()
    strategy_loss_streaks_mock.find_one = AsyncMock(return_value=None)
    strategy_loss_streaks_mock.update_one = AsyncMock()
    db.strategy_loss_streaks = strategy_loss_streaks_mock

    # Exit fill with negative P&L but exit_reason is take-profit
    fill_loss = {
        "id": "fill_1",
        "user_id": "user_1",
        "strategy_id": "strat_1",
        "symbol": "RELIANCE",
        "target_symbol": "RELIANCE",
        "side": "SELL",
        "qty": 10,
        "price": 90.0,  # Sell at 90 when bought at 100 -> loss
        "mode": "paper",
        "exit_reason": "take-profit",
        "charges": 1.0,
    }

    res = await ledger._apply_fill(fill_loss, "fill_1")
    assert res["accepted"] is True
    
    # Check that update_one was called with exit_reason overridden to stop-loss
    args, kwargs = db.strategy_positions.update_one.call_args
    update_doc = args[1]
    assert update_doc["$set"]["exit_reason"] == "stop-loss"
    assert fill_loss["exit_reason"] == "stop-loss"

    # Reset mock
    db.strategy_positions.update_one.reset_mock()

    # Exit fill with positive P&L but exit_reason is stop-loss
    fill_gain = {
        "id": "fill_2",
        "user_id": "user_1",
        "strategy_id": "strat_1",
        "symbol": "RELIANCE",
        "target_symbol": "RELIANCE",
        "side": "SELL",
        "qty": 10,
        "price": 110.0,  # Sell at 110 when bought at 100 -> gain
        "mode": "paper",
        "exit_reason": "stop-loss",
        "charges": 1.0,
    }

    res = await ledger._apply_fill(fill_gain, "fill_2")
    assert res["accepted"] is True
    
    # Check that update_one was called with exit_reason overridden to take-profit
    args, kwargs = db.strategy_positions.update_one.call_args
    update_doc = args[1]
    assert update_doc["$set"]["exit_reason"] == "take-profit"
    assert fill_gain["exit_reason"] == "take-profit"


@pytest.mark.asyncio
async def test_risk_manager_equity_sizing():
    # TASK-EQ-03: verify cash equity is sized by capital allocation
    db = MagicMock()
    risk = RiskManager(db)

    # Mock user and strategy settings
    db.users.find_one = AsyncMock(return_value={"id": "user_1", "settings": {}})
    db.strategies.find_one = AsyncMock(return_value={
        "id": "strat_1",
        "user_id": "user_1",
        "visual_config": {
            "risk": {
                "required_capital": 30000.0,  # ₹30,000 capital allocated
            }
        }
    })
    db.paper_wallets.find_one = AsyncMock(return_value={"balance": 500000.0})
    db.risk_state.find_one = AsyncMock(return_value={"_id": "global_kill_switch", "active": False})

    # Evaluate cash equity buy order (mock segment open check to return True)
    with patch("core.market_session_service.MarketSessionService.is_segment_open", return_value=True):
        res = await risk.evaluate_order(
            user_id="user_1",
            strategy_id="strat_1",
            symbol="RELIANCE",
            target_symbol="RELIANCE",
            side="BUY",
            requested_qty=1,  # Strategy signals 1 lot default
            price=1500.0,  # Price is 1500 per share
            mode="paper",
            stop_loss=1400.0,
            take_profit=1600.0,
            lot_size=1,
        )

    assert res["ok"] is True
    # Quantity should be sized to 30000 / 1500 = 20 shares!
    assert res["quantity"] == 20


@pytest.mark.asyncio
async def test_ops_enable_all_excludes_manual_paused():
    # TASK-EQ-05: verify manual toggle flags manual_paused and ops enable-all respects it
    db = MagicMock()

    # 1. Test strategy toggle manual pause
    db.strategies.find_one = AsyncMock(return_value={"id": "strat_1", "status": "live"})
    db.strategies.update_one = AsyncMock()
    
    with patch("routes.strategies.db", db), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": True}):
        res = await toggle_strategy("strat_1", user={"id": "user_1"})
        assert res["status"] == "paused"
        
        args, kwargs = db.strategies.update_one.call_args
        update_fields = args[1]["$set"]
        assert update_fields["manual_paused"] is True
        assert update_fields["schedule_paused"] is False

    # 2. Test ops enable-all query
    db.strategies.find.return_value.to_list = AsyncMock(return_value=[])
    db.strategies.update_many = AsyncMock(return_value=MagicMock(modified_count=0))

    with patch("routes.ops.db", db), \
         patch("server.option_ledger", create=True), \
         patch("server._sync_option_ledger_strategy", create=True), \
         patch("routes.ops.get_current_user", return_value={"id": "user_1"}):
        res = await ops_enable_all_strategies(user={"id": "user_1"})
        assert res["ok"] is True
        
        # Verify update_many query contains manual_paused filter
        args, kwargs = db.strategies.update_many.call_args
        query = args[0]
        assert query["manual_paused"] == {"$ne": True}
