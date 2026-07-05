"""IMD-04 — forward minute-bar capture (pure aggregation + store flush)."""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from core.options_minute_capture import MinuteBarAggregator, OptionsMinuteCapture  # noqa: E402
from core.options_minute_schema import OptionContractRef  # noqa: E402
from core.options_minute_store import OptionsMinuteStore  # noqa: E402

REF = OptionContractRef("NIFTY", "2025-01-09", 24200.0, "CE", "NSE_FO|K", "NSE_FO|K|exp")


def test_aggregator_builds_ohlc_and_rolls_minute():
    agg = MinuteBarAggregator()
    assert agg.add_tick("K", "2025-01-09T09:15:05+05:30", 100, cum_volume=10) is None
    assert agg.add_tick("K", "2025-01-09T09:15:30+05:30", 105, cum_volume=40) is None
    assert agg.add_tick("K", "2025-01-09T09:15:55+05:30", 98, cum_volume=60) is None
    # first tick of the next minute closes the 09:15 bar
    bar = agg.add_tick("K", "2025-01-09T09:16:02+05:30", 101, cum_volume=70)
    assert bar is not None
    assert bar.minute_ts == "2025-01-09T09:15:00+05:30"
    assert bar.open == 100 and bar.high == 105 and bar.low == 98 and bar.close == 98
    assert bar.volume == 60  # 60 - 0 start


def test_flush_closes_open_bar():
    agg = MinuteBarAggregator()
    agg.add_tick("K", "2025-01-09T09:15:05+05:30", 100, cum_volume=10)
    bars = agg.flush()
    assert len(bars) == 1 and bars[0].close == 100


@pytest.fixture()
def store():
    root = os.path.join(os.path.dirname(__file__), "_tmp_capture_store")
    shutil.rmtree(root, ignore_errors=True)
    yield OptionsMinuteStore(root=root)
    shutil.rmtree(root, ignore_errors=True)


def test_capture_writes_contract_day(store):
    cap = OptionsMinuteCapture(store, {"NSE_FO|K": REF})
    cap.on_tick("NSE_FO|K", "2025-01-09T09:15:05+05:30", 100, cum_volume=10, oi=250000)
    cap.on_tick("NSE_FO|K", "2025-01-09T09:16:05+05:30", 104, cum_volume=50, oi=250500)
    summary = cap.flush_day("2025-01-09", fetched_at="2026-07-06T00:00:00+05:30")
    assert summary["contracts_written"] == 1
    rows = store.get_option_minutes("upstox", "NIFTY", "2025-01-09", "2025-01-09", 24200.0, "CE")
    assert len(rows) == 2
    # a nearly-empty session should be flagged as a data-quality gap, not fabricated
    assert summary["data_quality_gaps"]["NSE_FO|K"].startswith("MISSING_")


def test_capture_ignores_unsubscribed_contract(store):
    cap = OptionsMinuteCapture(store, {"NSE_FO|K": REF})
    cap.on_tick("NSE_FO|OTHER", "2025-01-09T09:15:05+05:30", 100, cum_volume=10)
    assert cap.health()["ticks_seen"] == 0


def test_no_feed_no_bars(store):
    cap = OptionsMinuteCapture(store, {"NSE_FO|K": REF})
    summary = cap.flush_day("2025-01-09")
    assert summary["contracts_written"] == 0
    assert cap.health()["bars_written"] == 0


def test_health_counters(store):
    cap = OptionsMinuteCapture(store, {"NSE_FO|K": REF})
    cap.on_tick("NSE_FO|K", "2025-01-09T09:15:05+05:30", 100, cum_volume=10)
    h = cap.health()
    assert h["subscribed_contracts"] == 1 and h["ticks_seen"] == 1
    assert h["stale_feed_seconds"] is not None
