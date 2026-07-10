"""IMD-04 (live wiring) — LiveOptionCapture: ref registration from position/leg
docs, feed-tick capture for registered contracts only, and EOD flush."""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from core.live_option_capture import LiveOptionCapture  # noqa: E402
from core.options_minute_store import OptionsMinuteStore  # noqa: E402


@pytest.fixture()
def store():
    root = os.path.join(os.path.dirname(__file__), "_tmp_live_opt_store")
    shutil.rmtree(root, ignore_errors=True)
    yield OptionsMinuteStore(root=root)
    shutil.rmtree(root, ignore_errors=True)


SPREAD_POS = {
    "underlying": "NIFTY",
    "legs": [
        {"instrument_key": "NSE_FO|SHORT", "strike": 24000, "option_type": "PE",
         "expiry": "2025-01-09", "role": "short"},
        {"instrument_key": "NSE_FO|LONG", "strike": 23500, "option_type": "PE",
         "expiry": "2025-01-09", "role": "long"},
    ],
}


def test_registers_spread_legs_and_returns_new_keys(store):
    cap = LiveOptionCapture(store=store)
    new = cap.register_from_positions([SPREAD_POS])
    assert set(new) == {"NSE_FO|SHORT", "NSE_FO|LONG"}
    # idempotent — a second call registers nothing new
    assert cap.register_from_positions([SPREAD_POS]) == []


def test_skips_ungradeable_underlyings(store):
    cap = LiveOptionCapture(store=store)
    sensex = {"underlying": "SENSEX", "legs": [
        {"instrument_key": "BSE_FO|X", "strike": 78000, "option_type": "CE",
         "expiry": "2025-01-09", "role": "short"}]}
    assert cap.register_from_positions([sensex]) == []


def test_skips_legs_missing_metadata(store):
    cap = LiveOptionCapture(store=store)
    bad = {"underlying": "NIFTY", "legs": [
        {"instrument_key": "NSE_FO|Y", "option_type": "CE"}]}  # no strike/expiry
    assert cap.register_from_positions([bad]) == []


def test_only_registered_contracts_are_captured(store):
    cap = LiveOptionCapture(store=store)
    cap.register_from_positions([SPREAD_POS])
    # unregistered key is ignored (no raise, no capture)
    cap.on_tick("NSE_FO|UNKNOWN", {"received_at": "2025-01-09T09:15:05+05:30", "ltp": 5})
    assert cap.health()["ticks_seen"] == 0
    # registered key is captured
    cap.on_tick("NSE_FO|SHORT", {"received_at": "2025-01-09T09:15:05+05:30", "ltp": 42, "vtt": 10, "oi": 1000})
    assert cap.health()["ticks_seen"] == 1


def test_tick_to_flush_writes_a_contract_day(store):
    cap = LiveOptionCapture(store=store)
    cap.register_from_positions([SPREAD_POS])
    for sec, px, vol in [(5, 40, 10), (30, 45, 40), (55, 38, 60)]:
        cap.on_tick("NSE_FO|SHORT",
                    {"received_at": f"2025-01-09T09:15:{sec:02d}+05:30", "ltp": px, "vtt": vol, "oi": 900})
    res = cap.flush_day("2025-01-09")
    assert res["contracts_written"] >= 1
    assert res["bars_written"] >= 1
    rows = store.get_option_minutes("upstox", "NIFTY", "2025-01-09", "2025-01-09", 24000.0, "PE")
    assert rows  # the short leg's minute bar is now on disk


def test_malformed_tick_never_raises(store):
    cap = LiveOptionCapture(store=store)
    cap.register_from_positions([SPREAD_POS])
    # missing ltp / timestamp must be swallowed silently
    cap.on_tick("NSE_FO|SHORT", {"received_at": None, "ltp": None})
    cap.on_tick("NSE_FO|SHORT", {"ltp": ""})
    assert cap.health()["ticks_seen"] == 0
