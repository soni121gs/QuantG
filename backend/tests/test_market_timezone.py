"""
tests/test_market_timezone.py
Tests timezone correctness, Indian Standard Time (IST) market sessions,
and MCX/NSE gating boundaries.
"""
from __future__ import annotations

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

# Ensure backend root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.market_domains import DomainType
from core.market_session_service import MarketSessionService


class TestMarketTimezoneCorrectness(unittest.TestCase):

    def test_nse_closed_after_1530_ist(self):
        # 15:30 IST is 10:00 UTC. 15:31 IST is 10:01 UTC.
        # Let's verify NSE is closed at 15:31 IST on Monday (June 8, 2026).
        now_utc = datetime(2026, 6, 8, 10, 1, 0, tzinfo=timezone.utc)
        self.assertFalse(MarketSessionService.is_segment_open(DomainType.NSE_FO, now_utc))

    def test_nse_closed_before_0915_ist(self):
        # 09:15 IST is 03:45 UTC. 09:14 IST is 03:44 UTC.
        # Let's verify NSE is closed at 09:14 IST on Monday (June 8, 2026).
        now_utc = datetime(2026, 6, 8, 3, 44, 0, tzinfo=timezone.utc)
        self.assertFalse(MarketSessionService.is_segment_open(DomainType.NSE_FO, now_utc))

    def test_nse_open_during_0915_1530_ist_valid_weekday(self):
        # 09:15 IST is 03:45 UTC. Let's verify open at 09:15 IST on Monday.
        open_time_utc = datetime(2026, 6, 8, 3, 45, 0, tzinfo=timezone.utc)
        self.assertTrue(MarketSessionService.is_segment_open(DomainType.NSE_FO, open_time_utc))

        # 15:30 IST is 10:00 UTC. Let's verify open at 15:30 IST on Monday.
        close_time_utc = datetime(2026, 6, 8, 10, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(MarketSessionService.is_segment_open(DomainType.NSE_FO, close_time_utc))

        # Noon IST (12:00 IST is 06:30 UTC). Let's verify open.
        midday_utc = datetime(2026, 6, 8, 6, 30, 0, tzinfo=timezone.utc)
        self.assertTrue(MarketSessionService.is_segment_open(DomainType.NSE_FO, midday_utc))

    def test_weekend_closed(self):
        # Sunday, June 7, 2026. 12:00 IST (06:30 UTC).
        now_utc = datetime(2026, 6, 7, 6, 30, 0, tzinfo=timezone.utc)
        self.assertFalse(MarketSessionService.is_segment_open(DomainType.NSE_FO, now_utc))
        self.assertFalse(MarketSessionService.is_segment_open(DomainType.MCX_FO, now_utc))

    def test_utc_ist_conversion_safety(self):
        # Verify that a midnight UTC time (which converts to 05:30 IST) is closed for NSE.
        midnight_utc = datetime(2026, 6, 8, 0, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(MarketSessionService.is_segment_open(DomainType.NSE_FO, midnight_utc))

    def test_mcx_different_session_window(self):
        # MCX open at 09:00 IST (03:30 UTC) and close at 23:30 IST (18:00 UTC).
        # At 20:00 IST on Monday (14:30 UTC): NSE should be closed, but MCX should be open.
        now_utc = datetime(2026, 6, 8, 14, 30, 0, tzinfo=timezone.utc)
        self.assertFalse(MarketSessionService.is_segment_open(DomainType.NSE_FO, now_utc))
        self.assertTrue(MarketSessionService.is_segment_open(DomainType.MCX_FO, now_utc))


if __name__ == "__main__":
    unittest.main()
