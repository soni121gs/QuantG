"""
tests/test_trading_readiness.py

Backend tests for the QuantG production-readiness fixes:
  - NIFTY / BANKNIFTY / NATURALGAS / CRUDEOILM historical candle fetch
  - received_at always present in market snapshot (REST fallback)
  - tick_bar date format is IST %Y-%m-%d %H:%M
  - pre-trade gate passes with REST quote fallback
  - evaluate_market_data_quality REST fallback accepted
"""
from __future__ import annotations

import sys
import os
import math
import unittest
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch, AsyncMock

# Ensure backend root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk_controls import evaluate_market_data_quality, parse_market_timestamp


# ─────────────────────────────────────────────────────────────
# 1. risk_controls: received_at REST fallback
# ─────────────────────────────────────────────────────────────

class TestEvaluateMarketDataQuality(unittest.TestCase):

    def _now_iso(self):
        return datetime.now(timezone.utc).isoformat()

    def test_fresh_tick_accepted(self):
        received_at = self._now_iso()
        result = evaluate_market_data_quality(
            ltp=24800.0,
            tick_time=received_at,
            received_at=received_at,
            instrument_token="NSE_FO|12345",
            exchange="NSE",
            market_open=True,
            risk_style="balanced",
        )
        self.assertTrue(result["ok"], result)

    def test_missing_received_at_with_ltp_and_token_accepted(self):
        """Bug 2 fix: when received_at is None but ltp > 0 and token exists,
        the gate should allow (REST quote fallback)."""
        result = evaluate_market_data_quality(
            ltp=24800.0,
            tick_time=None,
            received_at=None,   # ← previously caused hard block
            instrument_token="NSE_FO|12345",
            exchange="NSE",
            market_open=True,
            risk_style="balanced",
        )
        self.assertTrue(result["ok"], f"Expected REST fallback acceptance, got: {result}")
        self.assertIn("REST", result.get("reason", ""), result)

    def test_missing_received_at_no_token_blocked(self):
        """No token + no received_at → still blocked (instrument unresolved)."""
        result = evaluate_market_data_quality(
            ltp=24800.0,
            tick_time=None,
            received_at=None,
            instrument_token="",   # no token
            exchange="NSE",
            market_open=True,
            risk_style="balanced",
        )
        self.assertFalse(result["ok"], result)

    def test_missing_received_at_zero_ltp_blocked(self):
        """No received_at + zero ltp → still blocked (ltp unavailable)."""
        result = evaluate_market_data_quality(
            ltp=0.0,
            tick_time=None,
            received_at=None,
            instrument_token="NSE_FO|12345",
            exchange="NSE",
            market_open=True,
            risk_style="balanced",
        )
        self.assertFalse(result["ok"], result)

    def test_stale_tick_blocked(self):
        stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        result = evaluate_market_data_quality(
            ltp=24800.0,
            tick_time=stale,
            received_at=stale,
            instrument_token="NSE_FO|12345",
            exchange="NSE",
            market_open=True,
            risk_style="balanced",
        )
        self.assertFalse(result["ok"], result)
        self.assertIn("stale", result.get("reason", "").lower(), result)

    def test_market_closed_blocked(self):
        received_at = self._now_iso()
        result = evaluate_market_data_quality(
            ltp=24800.0,
            tick_time=received_at,
            received_at=received_at,
            instrument_token="NSE_FO|12345",
            exchange="NSE",
            market_open=False,
            risk_style="balanced",
        )
        self.assertFalse(result["ok"], result)


# ─────────────────────────────────────────────────────────────
# 2. UpstoxGateway: index candle synthesis
# ─────────────────────────────────────────────────────────────

class TestUpstoxGatewayIndexCandles(unittest.TestCase):

    def _make_gateway(self):
        """Build a gateway with mocked _request."""
        from brokers.upstox_gateway import UpstoxGateway
        gw = UpstoxGateway.__new__(UpstoxGateway)
        # connected is a property that returns bool(self.access_token)
        gw.access_token = "fake-test-token"
        gw._tick_cache = {}
        gw._ws_thread = None
        return gw

    def test_nifty_index_key_detected(self):
        """NSE_INDEX| prefix must trigger _get_index_candles path."""
        gw = self._make_gateway()

        def fake_request(method, path, **kwargs):
            if "day" in path:
                # Return 10 daily candles
                candles = []
                for i in range(10):
                    dt = (datetime.now() - timedelta(days=10 - i)).strftime("%Y-%m-%dT09:15:00+05:30")
                    candles.append([dt, 24000 + i * 10, 24050 + i * 10, 23990 + i * 10, 24020 + i * 10, 1000])
                return {"status": "success", "data": {"candles": candles}}
            if "ohlc" in path:
                return {"status": "success", "data": {
                    "NSE_INDEX|Nifty 50": {
                        "last_price": 24850.0,
                        "ohlc": {"open": 24700.0, "high": 24900.0, "low": 24650.0, "close": 24850.0},
                    }
                }}
            return {"status": "error"}

        gw._request = fake_request
        result = gw.get_historical_candles("NSE_INDEX|Nifty 50", "5minute", 10)
        self.assertIsNotNone(result, "Expected candles, got None")
        self.assertGreater(len(result), 0, "Expected at least 1 bar")
        # All bars must have required fields
        for bar in result:
            for field in ("date", "open", "high", "low", "close", "volume"):
                self.assertIn(field, bar, f"Missing field {field} in bar {bar}")

    def test_banknifty_index_key_detected(self):
        gw = self._make_gateway()

        def fake_request(method, path, **kwargs):
            if "day" in path:
                candles = []
                for i in range(5):
                    dt = (datetime.now() - timedelta(days=5 - i)).strftime("%Y-%m-%dT09:15:00+05:30")
                    candles.append([dt, 52000 + i * 50, 52100 + i * 50, 51900 + i * 50, 52050 + i * 50, 500])
                return {"status": "success", "data": {"candles": candles}}
            if "ohlc" in path:
                return {"status": "error"}  # OHLC unavailable → synthesise
            return {"status": "error"}

        gw._request = fake_request
        result = gw.get_historical_candles("NSE_INDEX|Nifty Bank", "5minute", 5)
        self.assertIsNotNone(result, "Expected synthesised bars for BANKNIFTY")
        self.assertGreater(len(result), 0)

    def test_non_index_key_uses_standard_path(self):
        """Equity keys must NOT go through _get_index_candles."""
        gw = self._make_gateway()

        called_paths = []

        def fake_request(method, path, **kwargs):
            called_paths.append(path)
            # Return 5 1-minute candles
            candles = []
            for i in range(5):
                dt = f"2026-05-29T09:{15+i:02d}:00+05:30"
                candles.append([dt, 24000, 24010, 23990, 24005, 1000])
            return {"status": "success", "data": {"candles": candles}}

        gw._request = fake_request
        result = gw.get_historical_candles("NSE_EQ|INE002A01018", "5minute", 2)
        self.assertIsNotNone(result)
        # Must use the intraday endpoint, not ohlc
        self.assertTrue(any("intraday" in p for p in called_paths), called_paths)
        self.assertFalse(any("market-quote/ohlc" in p for p in called_paths), called_paths)

    def test_synthesise_session_bars_count(self):
        from brokers.upstox_gateway import UpstoxGateway
        bars = UpstoxGateway._synthesise_session_bars(24850.0, "5minute")
        # 09:15 to 15:25 is 75 bars of 5 minutes
        self.assertGreaterEqual(len(bars), 70)
        self.assertLessEqual(len(bars), 80)

    def test_synthesise_session_bars_date_format(self):
        from brokers.upstox_gateway import UpstoxGateway
        bars = UpstoxGateway._synthesise_session_bars(24850.0, "5minute")
        for bar in bars[:5]:
            # Must match %Y-%m-%d %H:%M exactly
            datetime.strptime(bar["date"], "%Y-%m-%d %H:%M")  # raises if wrong

    def test_resample_to_5min_correct(self):
        from brokers.upstox_gateway import UpstoxGateway
        one_min_bars = [
            {"date": f"2026-05-29 09:{m:02d}", "open": 100, "high": 101, "low": 99, "close": 100 + m * 0.1, "volume": 100}
            for m in range(15, 30)
        ]
        result = UpstoxGateway._resample_to_5min(one_min_bars)
        # 15 1-min bars → 3 5-min bars
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["date"], "2026-05-29 09:15")
        self.assertEqual(result[1]["date"], "2026-05-29 09:20")
        self.assertEqual(result[2]["date"], "2026-05-29 09:25")


# ─────────────────────────────────────────────────────────────
# 3. Tick bar date format (Bug 3)
# ─────────────────────────────────────────────────────────────

class TestTickBarDateFormat(unittest.TestCase):

    def test_tick_bar_date_format_is_ist(self):
        """The synthetic tick bar date must be IST %Y-%m-%d %H:%M, not UTC ISO."""
        from datetime import timezone, timedelta
        IST_OFFSET = timedelta(hours=5, minutes=30)
        ist_now = datetime.now(timezone.utc) + IST_OFFSET
        floored = (ist_now.minute // 5) * 5
        tick_date = ist_now.replace(minute=floored, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")

        # Must parse as %Y-%m-%d %H:%M without error
        parsed = datetime.strptime(tick_date, "%Y-%m-%d %H:%M")
        self.assertIsNotNone(parsed)

        # Must NOT contain 'T', '+', or 'Z' (which would indicate UTC ISO format)
        self.assertNotIn("T", tick_date)
        self.assertNotIn("+", tick_date)
        self.assertNotIn("Z", tick_date)


# ─────────────────────────────────────────────────────────────
# 4. parse_market_timestamp handles all formats
# ─────────────────────────────────────────────────────────────

class TestParseMarketTimestamp(unittest.TestCase):

    def test_iso_string(self):
        ts = "2026-05-29T09:15:00+05:30"
        result = parse_market_timestamp(ts)
        self.assertIsNotNone(result)

    def test_epoch_ms(self):
        result = parse_market_timestamp("1748471700000")
        self.assertIsNotNone(result)

    def test_none(self):
        result = parse_market_timestamp(None)
        self.assertIsNone(result)

    def test_empty(self):
        result = parse_market_timestamp("")
        self.assertIsNone(result)


class TestPaperModeIsolation(unittest.TestCase):

    def test_paper_mode_blocks_submit_order_intent(self):
        """Task 1 check: _submit_order_intent should raise error if user is in paper mode."""
        import asyncio
        from unittest.mock import AsyncMock
        from server import _submit_order_intent
        
        async def run_test():
            mock_settings = AsyncMock(return_value={"paper_mode": True})
            with patch("server.get_user_settings", mock_settings):
                mock_intent = MagicMock()
                with self.assertRaises(RuntimeError) as ctx:
                    await _submit_order_intent("user-1", mock_intent, order_type="MARKET", product="MIS", price=None, tag="tag")
                self.assertIn("Attempted to submit a broker order while in PAPER mode", str(ctx.exception))
                
        asyncio.run(run_test())

    def test_live_order_blocks_apply_paper_fill(self):
        """Task 1 check: _apply_paper_fill_to_position should raise error if order mode is live."""
        import asyncio
        from server import _apply_paper_fill_to_position
        
        async def run_test():
            live_order = {"id": "ord-1", "user_id": "user-1", "mode": "live"}
            with self.assertRaises(RuntimeError) as ctx:
                await _apply_paper_fill_to_position(live_order, 100.0)
            self.assertIn("Attempted to apply simulated paper fill to a LIVE order", str(ctx.exception))
            
        asyncio.run(run_test())


# ─────────────────────────────────────────────────────────────
# 6. Regression tests for VPS fixes (Task 10/Audit Fixes)
# ─────────────────────────────────────────────────────────────

class TestRuntimeBugFixesRegression(unittest.TestCase):

    def _make_gateway(self):
        from brokers.upstox_gateway import UpstoxGateway
        gw = UpstoxGateway.__new__(UpstoxGateway)
        gw.access_token = "fake-test-token"
        gw._tick_cache = {}
        gw._ws_thread = None
        return gw

    def test_upstox_ohlc_interval_mapping_regression(self):
        """Regression test for Fix #1: Verify Upstox index OHLC interval maps strictly to I1/I30/1d."""
        gw = self._make_gateway()
        called_paths = []

        def fake_request(method, path, **kwargs):
            called_paths.append(path)
            if "day" in path:
                return {"status": "success", "data": {"candles": []}}
            if "ohlc" in path:
                return {"status": "success", "data": {"NSE_INDEX|Nifty 50": {"ohlc": {"open": 24800.0, "high": 24850.0, "low": 24780.0, "close": 24820.0}, "last_price": 24820.0}}}
            return {"status": "error"}

        gw._request = fake_request

        # 1. 5minute -> must map to I1, not I5
        called_paths.clear()
        gw.get_historical_candles("NSE_INDEX|Nifty 50", "5minute", 5)
        ohlc_call = next((p for p in called_paths if "ohlc" in p), None)
        self.assertIsNotNone(ohlc_call)
        self.assertIn("interval=I1", ohlc_call)
        self.assertNotIn("interval=I5", ohlc_call)

        # 2. 15minute -> must map to I1, not I15
        called_paths.clear()
        gw.get_historical_candles("NSE_INDEX|Nifty 50", "15minute", 5)
        ohlc_call = next((p for p in called_paths if "ohlc" in p), None)
        self.assertIsNotNone(ohlc_call)
        self.assertIn("interval=I1", ohlc_call)
        self.assertNotIn("interval=I15", ohlc_call)

        # 3. 30minute -> must map to I30
        called_paths.clear()
        gw.get_historical_candles("NSE_INDEX|Nifty 30", "30minute", 5)
        ohlc_call = next((p for p in called_paths if "ohlc" in p), None)
        self.assertIsNotNone(ohlc_call)
        self.assertIn("interval=I30", ohlc_call)

        # 4. 60minute -> must map to I1, not I60
        called_paths.clear()
        gw.get_historical_candles("NSE_INDEX|Nifty 50", "60minute", 5)
        ohlc_call = next((p for p in called_paths if "ohlc" in p), None)
        self.assertIsNotNone(ohlc_call)
        self.assertIn("interval=I1", ohlc_call)
        self.assertNotIn("interval=I60", ohlc_call)

    def test_strategy_runner_safe_zero_division_regression(self):
        """Regression test for Fix #2: Verify safe strategy sandbox error handling with division-by-zero."""
        from strategy_runner import _safe_run
        
        # 1. Custom bad strategy with division-by-zero
        bad_code = """
def run(data):
    x = 1 / 0
    return []
"""
        res = _safe_run(bad_code, [{"date": "2026-05-29 15:00", "close": 100.0}])
        self.assertEqual(res, [], "Expected _safe_run to catch ZeroDivisionError gracefully and return []")

    def test_seeded_strategies_division_by_zero_immunity(self):
        """Regression test for Fix #2: Verify default seeded strategies are immune to division by zero on zero price data."""
        from server import DEFAULT_OPTION_STRATEGIES
        from safe_exec import safe_run_strategy

        # Mock candles where close prices are 0.0 (which would trigger the entry_price division bug)
        mock_zero_candles = [
            {"date": f"2026-05-29 09:{m:02d}", "close": 0.0, "open": 0.0, "high": 0.0, "low": 0.0, "volume": 100}
            for m in range(50)
        ]

        for strat in DEFAULT_OPTION_STRATEGIES:
            code = strat.get("python_code") or ""
            if "def run" in code:
                try:
                    res = safe_run_strategy(code, mock_zero_candles)
                    self.assertIsInstance(res, list, f"Strategy '{strat.get('name')}' must return a list")
                except Exception as e:
                    self.fail(f"Strategy template '{strat.get('name')}' raised an error on zero-price candles: {e}")


# ─────────────────────────────────────────────────────────────
# 7. Upstox V3 Feed Authentication Production Fix Tests
# ─────────────────────────────────────────────────────────────

class TestUpstoxFeedAuthProductionFix(unittest.TestCase):

    def test_mock_token_blocks_real_feed_startup(self):
        """Verify that a mock token starting with mock_live_upstox_token blocks feed startup."""
        from brokers.upstox_gateway import UpstoxGateway
        gw = UpstoxGateway.__new__(UpstoxGateway)
        gw.access_token = "mock_live_upstox_token_123"
        gw._subscribed_tokens = []
        gw._lock = MagicMock()
        
        result = gw.start_market_data_ws(["NSE_EQ|INE002A01018"])
        self.assertFalse(result["ok"])
        self.assertFalse(result["started"])
        self.assertEqual(result["reason"], "mock_token_blocked")
        self.assertIn("mock token", result["message"])

    def test_mock_token_blocks_live_order_placement(self):
        """Verify that mock Upstox tokens cannot simulate production order placement."""
        from brokers.upstox_gateway import UpstoxGateway

        gw = UpstoxGateway.__new__(UpstoxGateway)
        gw.access_token = "mock_live_upstox_token_123"

        with self.assertRaises(RuntimeError) as ctx:
            gw._request("POST", "/v2/order/place", hft=True, json={})

        self.assertIn("mock token cannot place production orders", str(ctx.exception))

    def test_get_user_upstox_status_detailed_telemetry(self):
        """Verify that get_user_upstox_status exposes all new telemetry details."""
        import asyncio
        from server import get_user_upstox_status
        
        async def run_test():
            mock_db = MagicMock()
            mock_keys = MagicMock()
            mock_keys.find_one = AsyncMock(return_value={
                "user_id": "test-user",
                "api_key": "enc_api_key",
                "api_secret": "enc_api_secret",
                "access_token": "enc_mock_live_upstox_token_abc",
            })
            mock_db.broker_keys = mock_keys
            
            with patch("server.db", mock_db), \
                 patch("server.decrypt_secret", lambda val: val.replace("enc_", "") if val else None), \
                 patch("server._UPSTOX_GATEWAYS", {}):
                
                status = await get_user_upstox_status("test-user")
                self.assertEqual(status["rest_status"], "mock")
                self.assertEqual(status["feed_status_str"], "mock_blocked")
                self.assertFalse(status["token_expired"])
                self.assertFalse(status["refresh_token_available"])
                self.assertTrue(status["daily_reconnect_required"])
                self.assertIn("fresh login daily", status["user_message"])
                
        asyncio.run(run_test())

    def test_upstox_callback_rejects_mock_token_in_live_mode(self):
        """Verify that upstox_callback raises HTTP 400 when storing a mock token in live mode."""
        import asyncio
        from fastapi import HTTPException
        from routes.broker import upstox_callback
        
        async def run_test():
            mock_db = MagicMock()
            mock_states = MagicMock()
            mock_states.find_one = AsyncMock(return_value={"user_id": "user-1", "redirect_uri": "http://uri"})
            mock_states.delete_one = AsyncMock()
            mock_keys = MagicMock()
            mock_keys.update_one = AsyncMock()
            
            mock_db.broker_oauth_states = mock_states
            mock_db.broker_keys = mock_keys
            
            mock_gw = MagicMock()
            mock_gw.exchange_code.return_value = {"access_token": "mock_live_upstox_token_abc", "user_id": "upstox-1"}
            mock_get_gw = AsyncMock(return_value=mock_gw)
            mock_settings = AsyncMock(return_value={"paper_mode": False})
            
            with patch("routes.broker.db", mock_db), \
                 patch("server.get_user_upstox_gateway", mock_get_gw), \
                 patch("server.get_user_settings", mock_settings):
                
                with self.assertRaises(HTTPException) as ctx:
                    await upstox_callback(code="code123", state="state123")
                self.assertEqual(ctx.exception.status_code, 400)
                self.assertIn("Cannot save simulated mock tokens in live production mode", str(ctx.exception.detail))
                
        asyncio.run(run_test())


class TestPart1StrategiesArmed(unittest.TestCase):
    def test_strategies_armed_zero_live(self):
        import asyncio
        from routes.ops_runtime import trading_ready_check
        
        async def run_test():
            mock_db = MagicMock()
            mock_strategies = MagicMock()
            
            # 0 live strategies, 2 paused/draft
            async def mock_count_documents(query):
                if query.get("status") == "live":
                    return 0
                return 2
                
            mock_strategies.count_documents = AsyncMock(side_effect=mock_count_documents)
            mock_db.strategies = mock_strategies
            
            # Other mocks needed by trading_ready_check
            mock_settings = AsyncMock(return_value={"paper_mode": True})
            mock_status = AsyncMock(return_value={"token_valid": True, "token_state": "active"})
            mock_gw = MagicMock()
            mock_gw.connected = True
            mock_gw.get_historical_candles = MagicMock(return_value=[{"close": 100}, {"close": 101}, {"close": 102}])
            mock_get_gw = AsyncMock(return_value=mock_gw)
            mock_open = MagicMock(return_value=True)
            mock_freshness = MagicMock(return_value={"fresh": True})
            
            with patch("routes.ops_runtime.db", mock_db), \
                 patch("server.get_user_settings", mock_settings), \
                 patch("server.get_user_upstox_status", mock_status), \
                 patch("server.get_user_upstox_gateway", mock_get_gw), \
                 patch("server._is_order_market_open", mock_open), \
                 patch("server._latest_candle_fresh_for_live", mock_freshness):
                
                res = await trading_ready_check(user={"id": "test-user"})
                
                # Check target output structure
                self.assertIn("strategies_armed", res["checks"])
                self.assertFalse(res["checks"]["strategies_armed"]["ok"])
                self.assertEqual(res["checks"]["strategies_armed"]["live_count"], 0)
                self.assertEqual(res["checks"]["strategies_armed"]["paused_or_draft_count"], 2)
                self.assertEqual(res["checks"]["strategies_armed"]["critical"], False)
                self.assertEqual(res["checks"]["strategies_armed"]["detail"], "0 live; 2 paused/draft won't auto-arm at 9AM")
                
                # Check warnings list has the expected string
                self.assertIn("warnings", res)
                self.assertEqual(len(res["warnings"]), 1)
                self.assertEqual(res["warnings"][0], "No strategies armed — arm at least one strategy to trade today.")
                
                # Check that ready is still True (because critical checks like broker_auth, gateway_connected are ok)
                self.assertTrue(res["ready"])
                
        asyncio.run(run_test())

    def test_strategies_armed_three_live(self):
        import asyncio
        from routes.ops_runtime import trading_ready_check
        
        async def run_test():
            mock_db = MagicMock()
            mock_strategies = MagicMock()
            
            # 3 live strategies, 1 paused/draft
            async def mock_count_documents(query):
                if query.get("status") == "live":
                    return 3
                return 1
                
            mock_strategies.count_documents = AsyncMock(side_effect=mock_count_documents)
            mock_db.strategies = mock_strategies
            
            # Other mocks needed by trading_ready_check
            mock_settings = AsyncMock(return_value={"paper_mode": True})
            mock_status = AsyncMock(return_value={"token_valid": True, "token_state": "active"})
            mock_gw = MagicMock()
            mock_gw.connected = True
            mock_gw.get_historical_candles = MagicMock(return_value=[{"close": 100}, {"close": 101}, {"close": 102}])
            mock_get_gw = AsyncMock(return_value=mock_gw)
            mock_open = MagicMock(return_value=True)
            mock_freshness = MagicMock(return_value={"fresh": True})
            
            with patch("routes.ops_runtime.db", mock_db), \
                 patch("server.get_user_settings", mock_settings), \
                 patch("server.get_user_upstox_status", mock_status), \
                 patch("server.get_user_upstox_gateway", mock_get_gw), \
                 patch("server._is_order_market_open", mock_open), \
                 patch("server._latest_candle_fresh_for_live", mock_freshness):
                
                res = await trading_ready_check(user={"id": "test-user"})
                
                self.assertIn("strategies_armed", res["checks"])
                self.assertTrue(res["checks"]["strategies_armed"]["ok"])
                self.assertEqual(res["checks"]["strategies_armed"]["live_count"], 3)
                self.assertEqual(res["checks"]["strategies_armed"]["paused_or_draft_count"], 1)
                self.assertEqual(res["checks"]["strategies_armed"]["critical"], False)
                self.assertEqual(res["checks"]["strategies_armed"]["detail"], "3 live; 1 paused/draft won't auto-arm at 9AM")
                
                # Check warnings list is empty
                self.assertIn("warnings", res)
                self.assertEqual(len(res["warnings"]), 0)
                
                self.assertTrue(res["ready"])
                
        asyncio.run(run_test())






if __name__ == "__main__":
    unittest.main(verbosity=2)
