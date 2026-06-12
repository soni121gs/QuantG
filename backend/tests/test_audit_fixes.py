import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

# Ensure backend is in the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trade_frequency import record_sl_hit, reset_streak_on_profit, check_frequency_gate
from core.portfolio_ledger import PortfolioLedger


@pytest.mark.anyio
async def test_loss_streak_standardization():
    """Verify that trade_frequency writes and reads current_streak instead of consecutive_sl_count."""
    mock_db = MagicMock()
    mock_db.strategy_loss_streaks.find_one = AsyncMock(return_value={
        "current_streak": 2,
        "paused_until": None,
    })
    
    # We mock update_one to capture what is being updated
    updated_doc = {}
    async def mock_update(query, update, upsert=False):
        nonlocal updated_doc
        updated_doc = update["$set"]
        
    mock_db.strategy_loss_streaks.update_one = mock_update
    
    # Trigger SL hit
    await record_sl_hit(mock_db, "strat-1", "user-1")
    
    # Check that current_streak was incremented to 3 and paused_until is set
    assert updated_doc.get("current_streak") == 3
    assert "paused_until" in updated_doc
    assert "consecutive_sl_count" not in updated_doc
    
    # Reset streak on profit
    await reset_streak_on_profit(mock_db, "strat-1", "user-1")
    assert updated_doc.get("current_streak") == 0
    assert updated_doc.get("paused_until") is None


@pytest.mark.anyio
async def test_portfolio_ledger_sets_streak_pause():
    """Verify that PortfolioLedger manages current_streak and paused_until on negative P&L full close."""
    mock_db = MagicMock()
    
    # 1. Existing streak document shows 2 consecutive losses
    mock_db.strategy_loss_streaks.find_one = AsyncMock(return_value={
        "current_streak": 2,
        "paused_until": None,
    })
    
    # Track updates
    updated_streak = {}
    async def mock_streak_update(query, update, upsert=False):
        nonlocal updated_streak
        updated_streak = update["$set"]
        
    mock_db.strategy_loss_streaks.update_one = mock_streak_update
    
    # Mock position to close (LONG with average entry 100)
    pos = {
        "id": "pos-1",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26FEB24850CE",
        "mode": "paper",
        "position_side": "LONG",
        "open_quantity": 50,
        "quantity": 50,
        "average_buy_price": 100.0,
        "status": "OPEN",
        "entry_charges": 20.0,
    }
    
    mock_db.strategy_positions.find_one = AsyncMock(return_value=pos)
    mock_db.strategy_positions.update_one = AsyncMock()
    mock_db.positions.delete_one = AsyncMock()
    mock_db.trade_fills.insert_one = AsyncMock()
    mock_db.trades.insert_one = AsyncMock()
    mock_db.strategies.update_one = AsyncMock()
    mock_db.processed_fill_ids.insert_one = AsyncMock()
    
    # Initialize ledger
    ledger = PortfolioLedger(mock_db)
    
    # Process exit fill at 90 (loss)
    fill = {
        "id": "fill-exit-1",
        "order_id": "order-exit-1",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26FEB24850CE",
        "side": "SELL",
        "qty": 50,
        "price": 90.0,
        "charges": 10.0,
        "brokerage": 10.0,
        "mode": "paper",
        "exit_reason": "stop_loss",
    }
    
    res = await ledger.process_fill(fill)
    assert res["accepted"] is True
    assert res["action"] == "CLOSE"
    
    # Ensure current_streak is incremented to 3 and paused_until is set
    assert updated_streak.get("current_streak") == 3
    assert "paused_until" in updated_streak


@pytest.mark.anyio
async def test_paper_wallet_queries_trade_fills():
    """Verify that get_paper_wallet retrieves recent fills from db.trade_fills, not db.fills."""
    import server
    
    mock_db = MagicMock()
    mock_db.trade_fills.find = MagicMock()
    mock_db.fills.find = MagicMock()
    
    # Set up paper wallet mock responses
    mock_db.paper_wallets.find_one = AsyncMock(return_value={
        "user_id": "user-1",
        "balance": 100000.0,
        "initial_capital": 100000.0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    
    # Mock find().sort().limit().to_list() chain
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.limit.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(return_value=[])
    mock_db.trade_fills.find.return_value = mock_cursor
    
    with patch("server.db", mock_db):
        res = await server.get_paper_wallet(user={"id": "user-1"})
        
    assert res["ok"] is True
    mock_db.trade_fills.find.assert_called_once()
    mock_db.fills.find.assert_not_called()


@pytest.mark.anyio
async def test_book_live_fill_routes_to_portfolio_ledger():
    """Verify that _book_live_fill_from_order routes the fill doc through PortfolioLedger."""
    import server
    
    mock_db = MagicMock()
    mock_db.trade_fills.find_one = AsyncMock(return_value=None)
    mock_db.orders.update_one = AsyncMock()
    
    order = {
        "id": "ord-live-1",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "side": "BUY",
        "qty": 50,
        "price": 100.0,
        "mode": "live",
        "order_intent": {
            "target_symbol": "NIFTY26FEB24850CE",
            "instrument_key": "NSE_FO|12345",
            "product": "MIS",
        }
    }
    
    mock_ledger_instance = MagicMock()
    mock_ledger_instance.process_fill = AsyncMock(return_value={
        "accepted": True,
        "action": "OPEN",
        "position_id": "pos-live-1",
        "realized_pnl": 0.0,
        "gross_pnl": 0.0,
    })
    
    with patch("server.db", mock_db), patch("core.portfolio_ledger.PortfolioLedger", return_value=mock_ledger_instance):
        res = await server._book_live_fill_from_order(order, fill_price=100.0, filled_qty=50)
        
    assert res is not None
    assert res["realized_pnl"] == 0.0
    mock_ledger_instance.process_fill.assert_called_once()
    mock_db.orders.update_one.assert_called_once()
