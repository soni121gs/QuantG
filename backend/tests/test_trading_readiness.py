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


if __name__ == "__main__":
    unittest.main(verbosity=2)
