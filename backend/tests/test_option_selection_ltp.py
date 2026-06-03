"""
tests/test_option_selection_ltp.py

Tests for QuantG option contract selection, option LTP retrieval, and robust order placement logging.
"""
from __future__ import annotations

import sys
import os
import unittest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock

# Ensure backend root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brokers.upstox_gateway import UpstoxGateway
from server import _resolve_option_for_strategy, _place_upstox_order

class TestOptionSelectionLtp(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        # Setup mock db and strategy row
        self.db = MagicMock()
        self.strategy_row = {"mode": "paper", "id": "test-strategy-1"}
        self.user_id = "test-user-id"

    @patch("server.get_user_settings", new_callable=AsyncMock)
    @patch("server.get_user_upstox_gateway", new_callable=AsyncMock)
    async def test_resolve_option_for_strategy_paper_fallback_disabled(self, mock_get_gateway, mock_get_settings):
        """Verify fabricated paper contracts stay blocked when simulation is disabled."""
        mock_get_settings.return_value = {"paper_mode": True, "allow_simulated_prices": False}
        mock_get_gateway.return_value = None  # Gateway offline

        contract = await _resolve_option_for_strategy(
            user_id=self.user_id,
            strategy_row=self.strategy_row,
            underlying="NIFTY",
            signal_action="BUY",
            strike_mode="ATM_BUY",
        )

        self.assertIsNone(contract)

    @patch("server.get_user_settings", new_callable=AsyncMock)
    @patch("server.get_user_upstox_gateway", new_callable=AsyncMock)
    async def test_resolve_option_for_strategy_paper_simulated_contract(self, mock_get_gateway, mock_get_settings):
        """Paper mode can use a complete simulated contract when explicitly enabled."""
        mock_get_settings.return_value = {"paper_mode": True, "allow_simulated_prices": True}
        mock_get_gateway.return_value = None

        contract = await _resolve_option_for_strategy(
            user_id=self.user_id,
            strategy_row=self.strategy_row,
            underlying="NIFTY",
            signal_action="BUY",
            strike_mode="ATM_BUY",
        )

        self.assertIsNotNone(contract)
        self.assertTrue(contract["simulated"])
        self.assertEqual(contract["source"], "PAPER_SIMULATED_CONTRACT")
        self.assertTrue(contract["instrument_token"].startswith("PAPER_NIFTY_CE_"))
        self.assertEqual(contract["instrument_key"], contract["instrument_token"])
        self.assertEqual(contract["upstox_instrument_token"], contract["instrument_token"])
        self.assertGreater(contract["ltp"], 0)

    @patch("server.get_user_settings", new_callable=AsyncMock)
    @patch("server.get_user_upstox_gateway", new_callable=AsyncMock)
    async def test_resolve_option_for_strategy_live_never_simulates(self, mock_get_gateway, mock_get_settings):
        """Live strategies must not receive fabricated paper contracts."""
        mock_get_settings.return_value = {"paper_mode": False, "allow_simulated_prices": True}
        mock_get_gateway.return_value = None

        contract = await _resolve_option_for_strategy(
            user_id=self.user_id,
            strategy_row={"mode": "live", "id": "live-strategy-1"},
            underlying="NIFTY",
            signal_action="BUY",
            strike_mode="ATM_BUY",
        )

        self.assertIsNone(contract)

    @patch("server.get_user_settings", new_callable=AsyncMock)
    @patch("server.get_user_upstox_gateway", new_callable=AsyncMock)
    async def test_resolve_option_for_strategy_live_chain_success(self, mock_get_gateway, mock_get_settings):
        """Verify successful option chain lookup and LTP assignment when gateway is connected."""
        mock_get_settings.return_value = {"paper_mode": True}
        
        # Mock Upstox Gateway
        gw = UpstoxGateway.__new__(UpstoxGateway)
        gw.access_token = "fake-token"
        gw._tick_cache = {}
        
        # Mock get_market_quote for Nifty spot
        def fake_get_market_quote(keys):
            if "NSE_INDEX|Nifty 50" in keys:
                return {"status": "success", "data": {"NSE_INDEX|Nifty 50": {"last_price": 24853.20}}}
            # Option token
            return {"status": "success", "data": {"NSE_FO|123456": {"last_price": 45.50}}}
            
        gw.get_market_quote = fake_get_market_quote

        # Mock get_option_chain
        def fake_get_option_chain(underlying_key, expiry_date):
            return {
                "status": "success",
                "data": [
                    {
                        "strike_price": 24900.00,
                        "expiry": "2026-06-05",
                        "call_options": {
                            "instrument_key": "NSE_FO|123456",
                            "trading_symbol": "NIFTY2660524900CE"
                        }
                    }
                ]
            }
        gw.get_option_chain = fake_get_option_chain

        # Mock latest_tick
        gw.latest_tick = MagicMock(return_value={"ltp": 45.50})
        
        mock_get_gateway.return_value = gw

        contract = await _resolve_option_for_strategy(
            user_id=self.user_id,
            strategy_row=self.strategy_row,
            underlying="NIFTY",
            signal_action="BUY",
            strike_mode="ATM_BUY",
            otm_points=50,  # 24850 atm + 50 otm = 24900
        )

        self.assertIsNotNone(contract)
        self.assertEqual(contract["instrument_token"], "NSE_FO|123456")
        self.assertEqual(contract["tradingsymbol"], "NIFTY2660524900CE")
        self.assertEqual(contract["expiry"], "2026-06-05")
        self.assertEqual(contract["ltp"], 45.50)

    @patch("server.get_user_settings", new_callable=AsyncMock)
    @patch("server.get_user_upstox_gateway", new_callable=AsyncMock)
    async def test_real_contract_without_quote_does_not_get_mock_34_ltp(self, mock_get_gateway, mock_get_settings):
        """A real Upstox contract must not be stamped with the old hardcoded paper LTP."""
        mock_get_settings.return_value = {"paper_mode": True, "allow_simulated_prices": True}

        gw = UpstoxGateway.__new__(UpstoxGateway)
        gw.access_token = "fake-token"

        def fake_get_market_quote(keys):
            if "NSE_INDEX|Nifty 50" in keys:
                return {"status": "success", "data": {"NSE_INDEX|Nifty 50": {"last_price": 24853.20}}}
            return {"status": "success", "data": {"NSE_FO|123456": {"last_price": None}}}

        def fake_get_option_chain(underlying_key, expiry_date):
            return {
                "status": "success",
                "data": [{
                    "strike_price": 24900.00,
                    "expiry": "2026-06-05",
                    "call_options": {
                        "instrument_key": "NSE_FO|123456",
                        "trading_symbol": "NIFTY2660524900CE",
                    },
                }],
            }

        gw.get_market_quote = fake_get_market_quote
        gw.get_option_chain = fake_get_option_chain
        gw.latest_tick = MagicMock(return_value=None)
        mock_get_gateway.return_value = gw

        contract = await _resolve_option_for_strategy(
            user_id=self.user_id,
            strategy_row=self.strategy_row,
            underlying="NIFTY",
            signal_action="BUY",
            strike_mode="ATM_BUY",
            otm_points=50,
        )

        self.assertIsNotNone(contract)
        self.assertFalse(contract["simulated"])
        self.assertEqual(contract["instrument_token"], "NSE_FO|123456")
        self.assertNotIn("ltp", contract)

    @patch("server.get_user_upstox_gateway", new_callable=AsyncMock)
    async def test_place_upstox_order_rejection_payload_logging(self, mock_get_gateway):
        """Verify order payload logging on exception during order placement."""
        gw = UpstoxGateway.__new__(UpstoxGateway)
        gw.access_token = "fake-token"
        
        def fake_place_order(*args, **kwargs):
            raise Exception("UDAPI10011 Invalid Instrument key")
            
        gw.place_order = fake_place_order
        mock_get_gateway.return_value = gw

        with self.assertRaises(Exception) as ctx:
            await _place_upstox_order(
                user_id=self.user_id,
                instrument_token="NSE_FO|99999",
                side="BUY",
                quantity=50,
                order_type="MARKET",
                product="MIS",
            )
        
        self.assertIn("UDAPI10011", str(ctx.exception))

if __name__ == "__main__":
    unittest.main(verbosity=2)
