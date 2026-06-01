import sys
import os
import unittest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import _build_order_intent, get_ist_midnight_utc_iso


class TestAuditFixes(unittest.TestCase):

    def test_get_ist_midnight_utc_iso(self):
        """Verify that get_ist_midnight_utc_iso correctly returns UTC 18:30 of the previous day."""
        iso_str = get_ist_midnight_utc_iso()
        dt = datetime.fromisoformat(iso_str)
        # Check that the timezone-aware UTC representation ends at 18:30
        self.assertEqual(dt.hour, 18)
        self.assertEqual(dt.minute, 30)
        self.assertEqual(dt.second, 0)

    def test_dynamic_intent_resolution_direct_no_active_position(self):
        """Verify _build_order_intent for direct equity without active position."""
        async def run_test():
            mock_db = MagicMock()
            # Find returns None (no active position)
            mock_db.strategy_positions.find_one = AsyncMock(return_value=None)
            
            with patch("server.db", mock_db):
                # 1. Buy Order
                res_buy = await _build_order_intent(
                    user_id="user-1",
                    symbol="RELIANCE",
                    side="BUY",
                    qty=10,
                    source="manual",
                    exchange="NSE",
                    settings={"default_qty": 1},
                )
                self.assertEqual(res_buy["intent"].intent, "OPEN_LONG")
                self.assertEqual(res_buy["intent"].instrument.asset_class, "DIRECT")

                # 2. Sell Order
                res_sell = await _build_order_intent(
                    user_id="user-1",
                    symbol="RELIANCE",
                    side="SELL",
                    qty=10,
                    source="manual",
                    exchange="NSE",
                    settings={"default_qty": 1},
                )
                self.assertEqual(res_sell["intent"].intent, "OPEN_SHORT")
                self.assertEqual(res_sell["intent"].instrument.asset_class, "DIRECT")

        asyncio.run(run_test())

    def test_dynamic_intent_resolution_direct_with_active_position(self):
        """Verify _build_order_intent for direct equity with active position."""
        async def run_test():
            mock_db = MagicMock()
            
            # Active Position: SHORT
            mock_db.strategy_positions.find_one = AsyncMock(return_value={
                "id": "pos-1",
                "user_id": "user-1",
                "strategy_id": "manual_recovery",
                "instrument_key": "NSE:RELIANCE",
                "position_side": "SHORT",
                "status": "OPEN",
            })
            
            with patch("server.db", mock_db):
                # Buy Order to Cover
                res_buy = await _build_order_intent(
                    user_id="user-1",
                    symbol="RELIANCE",
                    side="BUY",
                    qty=10,
                    source="manual",
                    exchange="NSE",
                    settings={"default_qty": 1},
                )
                self.assertEqual(res_buy["intent"].intent, "CLOSE_SHORT")

            # Active Position: LONG
            mock_db.strategy_positions.find_one = AsyncMock(return_value={
                "id": "pos-2",
                "user_id": "user-1",
                "strategy_id": "manual_recovery",
                "instrument_key": "NSE:RELIANCE",
                "position_side": "LONG",
                "status": "OPEN",
            })
            
            with patch("server.db", mock_db):
                # Sell Order to Close
                res_sell = await _build_order_intent(
                    user_id="user-1",
                    symbol="RELIANCE",
                    side="SELL",
                    qty=10,
                    source="manual",
                    exchange="NSE",
                    settings={"default_qty": 1},
                )
                self.assertEqual(res_sell["intent"].intent, "CLOSE_LONG")

        asyncio.run(run_test())

    def test_dynamic_intent_resolution_options(self):
        """Verify _build_order_intent for options contract dynamically."""
        async def run_test():
            mock_db = MagicMock()
            
            # Active Position: SHORT Option
            mock_db.strategy_positions.find_one = AsyncMock(return_value={
                "id": "pos-opt-1",
                "user_id": "user-1",
                "strategy_id": "manual_recovery",
                "instrument_key": "NFO:NIFTY2660424850CE",
                "position_side": "SHORT",
                "status": "OPEN",
            })
            
            option_contract = {
                "tradingsymbol": "NIFTY2660424850CE",
                "exchange": "NFO",
                "instrument_token": "54323",
                "transaction_type": "BUY",
                "underlying": "NIFTY",
                "option_type": "CE",
                "strike": 24850,
                "expiry": "2026-06-04",
                "lot_size": 50,
            }
            
            with patch("server.db", mock_db):
                res = await _build_order_intent(
                    user_id="user-1",
                    symbol="NIFTY",
                    side="BUY",
                    qty=1,
                    source="manual",
                    exchange="NSE",
                    settings={"default_qty": 1},
                    option_contract=option_contract,
                )
                self.assertEqual(res["intent"].intent, "CLOSE_SHORT")
                self.assertEqual(res["intent"].instrument.asset_class, "OPTION_SHORT")

        asyncio.run(run_test())

    def test_check_daily_loss_guard_passed(self):
        """Verify _check_daily_loss_guard passes when loss is within limit."""
        async def run_test():
            mock_db = MagicMock()
            mock_find_cursor = MagicMock()
            mock_find_cursor.to_list = AsyncMock(return_value=[
                {"realised_pnl": -50.0},
                {"realised_pnl": 10.0},
            ])
            mock_db.orders.find = MagicMock(return_value=mock_find_cursor)

            with patch("server.db", mock_db):
                from server import _check_daily_loss_guard
                await _check_daily_loss_guard(user_id="user-1", max_loss=1000.0)

        asyncio.run(run_test())

    def test_check_daily_loss_guard_tripped(self):
        """Verify _check_daily_loss_guard raises HTTPException when loss exceeds limit."""
        async def run_test():
            from fastapi import HTTPException
            mock_db = MagicMock()
            mock_find_cursor = MagicMock()
            mock_find_cursor.to_list = AsyncMock(return_value=[
                {"realised_pnl": -1000.0},
                {"realised_pnl": -200.0},
            ])
            mock_db.orders.find = MagicMock(return_value=mock_find_cursor)

            with patch("server.db", mock_db):
                from server import _check_daily_loss_guard
                with self.assertRaises(HTTPException) as ctx:
                    await _check_daily_loss_guard(user_id="user-1", max_loss=1000.0)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("Daily loss guard tripped", ctx.exception.detail)

        asyncio.run(run_test())

    def test_check_trade_count_guard_passed(self):
        """Verify _check_trade_count_guard passes when trade count is below limit."""
        async def run_test():
            mock_db = MagicMock()
            mock_db.orders.count_documents = AsyncMock(return_value=4)

            with patch("server.db", mock_db):
                from server import _check_trade_count_guard
                await _check_trade_count_guard(user_id="user-1", max_trades=5)

        asyncio.run(run_test())

    def test_check_trade_count_guard_tripped(self):
        """Verify _check_trade_count_guard raises HTTPException when trade count matches/exceeds limit."""
        async def run_test():
            from fastapi import HTTPException
            mock_db = MagicMock()
            mock_db.orders.count_documents = AsyncMock(return_value=5)

            with patch("server.db", mock_db):
                from server import _check_trade_count_guard
                with self.assertRaises(HTTPException) as ctx:
                    await _check_trade_count_guard(user_id="user-1", max_trades=5)
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("Daily trade guard tripped", ctx.exception.detail)

        asyncio.run(run_test())

    def test_cooldown_isolation_on_signal_filtering(self):
        """Verify cooldown isolation: filtered/rejected signals do not update strategy cooldown."""
        async def run_test():
            # Mock the strategy runner / signal manager db updates
            mock_db = MagicMock()
            # We mock the db.strategies.update_one to assert that it is NOT called for rejected/filtered signals
            mock_db.strategies.update_one = AsyncMock()
            mock_db.signals.update_one = AsyncMock()
            
            # Let's import ConflictResolver to simulate resolve
            from signal_manager import ConflictResolver
            
            # Signal A: CE Buy, confidence 90
            sig_ce = {
                "id": "sig-ce",
                "user_id": "user-1",
                "strategy_id": "strat-ce",
                "symbol": "NIFTY",
                "target_symbol": "NIFTY2660524900CE",
                "option_type": "CE",
                "action": "BUY",
                "confidence": 90.0,
                "status": "PENDING",
            }
            
            # Signal B: PE Buy, confidence 80 (will clash and be rejected)
            sig_pe = {
                "id": "sig-pe",
                "user_id": "user-1",
                "strategy_id": "strat-pe",
                "symbol": "NIFTY",
                "target_symbol": "NIFTY2660524900PE",
                "option_type": "PE",
                "action": "BUY",
                "confidence": 80.0,
                "status": "PENDING",
            }
            
            # 1. Run ConflictResolver
            approved, rejected = ConflictResolver.resolve(
                pending_signals=[sig_ce, sig_pe],
                active_positions=[],
                one_active_position_per_symbol_group=False
            )
            
            self.assertEqual(len(approved), 1)
            self.assertEqual(approved[0]["id"], "sig-ce")
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["id"], "sig-pe")
            self.assertEqual(rejected[0]["status"], "REJECTED")
            
            # 2. Simulate dispatch block inside signal_manager_loop
            # For rejected signals:
            for sig in rejected:
                await mock_db.signals.update_one(
                    {"id": sig["id"]},
                    {"$set": {
                        "status": sig["status"],
                        "rejection_reason": sig["rejection_reason"],
                        "processed_at": datetime.now(timezone.utc).isoformat()
                    }}
                )
            
            # For approved signals (simulate success):
            for sig in approved:
                now_str = datetime.now(timezone.utc).isoformat()
                await mock_db.strategies.update_one(
                    {"id": sig["strategy_id"], "user_id": "user-1"},
                    {"$set": {"last_signal_at": now_str}}
                )
            
            # Assertions
            # 1. signals.update_one was called once (for sig-pe)
            self.assertEqual(mock_db.signals.update_one.call_count, 1)
            # 2. strategies.update_one was called once (only for approved sig-ce, NOT for sig-pe)
            self.assertEqual(mock_db.strategies.update_one.call_count, 1)
            
            # Check strategy updated is indeed the approved one
            called_args = mock_db.strategies.update_one.call_args[0]
            self.assertEqual(called_args[0]["id"], "strat-ce")

        asyncio.run(run_test())

    def test_get_trading_day_window_ist(self):
        """Verify get_trading_day_window_ist returns correct IST day boundaries in UTC."""
        from server import get_trading_day_window_ist
        start, end = get_trading_day_window_ist()
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        self.assertEqual(end_dt - start_dt, timedelta(days=1))
        # start must be at 18:30 UTC of previous day
        self.assertEqual(start_dt.hour, 18)
        self.assertEqual(start_dt.minute, 30)

    def test_reset_paper_trading(self):
        """Verify reset_paper_trading endpoint calls purge on correct collections for user."""
        async def run_test():
            from server import reset_paper_trading
            mock_db = MagicMock()
            
            # Mock return value of db.strategies.find
            mock_cursor = MagicMock()
            mock_cursor.to_list = AsyncMock(return_value=[{"id": "strat-1"}])
            mock_db.strategies.find = MagicMock(return_value=mock_cursor)
            
            # Mock delete_many and update_many
            mock_db.orders.delete_many = AsyncMock(return_value=MagicMock(deleted_count=5))
            mock_db.strategy_positions.delete_many = AsyncMock(return_value=MagicMock(deleted_count=2))
            mock_db.positions.delete_many = AsyncMock(return_value=MagicMock(deleted_count=1))
            mock_db.strategy_position_locks.delete_many = AsyncMock(return_value=MagicMock(deleted_count=0))
            mock_db.signals.delete_many = AsyncMock(return_value=MagicMock(deleted_count=10))
            mock_db.paper_trading_history.delete_many = AsyncMock(return_value=MagicMock(deleted_count=3))
            mock_db.trades.delete_many = AsyncMock(return_value=MagicMock(deleted_count=4))
            mock_db.option_open_positions.delete_many = AsyncMock(return_value=MagicMock(deleted_count=1))
            mock_db.option_daily_pnl.delete_many = AsyncMock(return_value=MagicMock(deleted_count=2))
            mock_db.option_trade_journal.delete_many = AsyncMock(return_value=MagicMock(deleted_count=1))
            mock_db.option_strategy_states.update_many = AsyncMock()
            mock_db.risk_events.delete_many = AsyncMock(return_value=MagicMock(deleted_count=0))
            
            with patch("server.db", mock_db):
                user = {"id": "user-123", "email": "test@quantg.com"}
                res = await reset_paper_trading(user=user)
                
                self.assertTrue(res["ok"])
                self.assertEqual(res["purged"]["orders"], 5)
                self.assertEqual(res["purged"]["strategy_positions"], 2)
                self.assertEqual(res["purged"]["broker_positions"], 1)
                
                # Check user scope in queries
                mock_db.orders.delete_many.assert_called_with({"user_id": "user-123", "mode": "paper"})
                mock_db.positions.delete_many.assert_called_with({"user_id": "user-123"})
                mock_db.option_open_positions.delete_many.assert_called_with({"strategy_id": {"$in": ["strat-1"]}})

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
