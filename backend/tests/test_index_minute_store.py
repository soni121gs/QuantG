"""Index 1-minute store + live index capture."""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from core.index_minute_store import IndexMinuteStore  # noqa: E402
from core.live_index_capture import LiveIndexCapture  # noqa: E402


@pytest.fixture()
def store():
    root = os.path.join(os.path.dirname(__file__), "_tmp_index_store")
    shutil.rmtree(root, ignore_errors=True)
    yield IndexMinuteStore(root=root)
    shutil.rmtree(root, ignore_errors=True)


def test_normalize_and_roundtrip(store):
    raw = [
        {"date": "2025-01-09 09:15", "open": 24000, "high": 24010, "low": 23990, "close": 24005, "volume": 0},
        {"date": "2025-01-09 09:16", "open": 24005, "high": 24020, "low": 24000, "close": 24018, "volume": 0},
    ]
    by_day = store.normalize_candles("NIFTY", raw)
    assert list(by_day.keys()) == ["2025-01-09"]
    store.write_day("NIFTY", "2025-01-09", by_day["2025-01-09"])
    got = store.get_minutes("NIFTY", "2025-01-09")
    assert len(got) == 2
    assert got[0]["close"] == 24005.0
    assert got[0]["date"] == got[0]["timestamp_ist"]  # signal code reads 'date'
    assert store.trading_days("NIFTY") == ["2025-01-09"]


def test_missing_day_empty(store):
    assert store.get_minutes("NIFTY", "2099-01-01") == []


def test_live_index_capture_aggregates_and_flushes(store):
    cap = LiveIndexCapture(store=store)
    cap.on_tick("NSE_INDEX|Nifty 50", {"received_at": "2025-01-09T09:15:05+05:30", "ltp": 24000, "vtt": 0})
    cap.on_tick("NSE_INDEX|Nifty 50", {"received_at": "2025-01-09T09:15:40+05:30", "ltp": 24012, "vtt": 0})
    cap.on_tick("NSE_INDEX|Nifty 50", {"received_at": "2025-01-09T09:16:05+05:30", "ltp": 24008, "vtt": 0})
    res = cap.flush_day("2025-01-09")
    assert res["underlyings_written"] == 1
    rows = store.get_minutes("NIFTY", "2025-01-09")
    assert len(rows) == 2
    assert rows[0]["high"] == 24012.0


def test_live_capture_ignores_non_index(store):
    cap = LiveIndexCapture(store=store)
    cap.on_tick("NSE_FO|12345", {"received_at": "2025-01-09T09:15:05+05:30", "ltp": 100})
    assert cap.health()["ticks_seen"] == 0
