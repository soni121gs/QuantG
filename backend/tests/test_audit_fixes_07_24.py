"""Regression guards for the 2026-07-24 full-system audit fixes.

Each test pins a defect that was live in production that day, so it cannot return
silently. See CLAUDE.md §22 and memory project_full_system_audit_07_24.
"""
import asyncio

import pytest

from core.bhavcopy_store import BhavcopyStore, _ROWS_CACHE_BUDGET
from core.dynamic_contract_selector import select_dynamic_credit_spread
from core.hermes_diagnostics.probe_sdk import ProbeContext
from core.hermes_diagnostics.probes_infra import overgated_book, process_restarts
from core.live_index_capture import LiveIndexCapture
from core.regime_classifier import classify_intraday


class _Bar:
    def __init__(self, key, ts, px):
        self.instrument_key = key
        self.minute_ts = ts
        self.open = self.high = self.low = self.close = px
        self.volume = 0
        self.last_cum = 0
        self.start_cum = 0


def _cap_with_three_open():
    cap = LiveIndexCapture()
    cap.agg._open = {
        "NSE_INDEX|Nifty 50": _Bar("NSE_INDEX|Nifty 50", "2026-07-24 09:45", 23700.0),
        "NSE_INDEX|Nifty Bank": _Bar("NSE_INDEX|Nifty Bank", "2026-07-24 09:45", 56400.0),
        "BSE_INDEX|SENSEX": _Bar("BSE_INDEX|SENSEX", "2026-07-24 09:45", 75900.0),
    }
    return cap


def test_snapshot_minutes_does_not_leak_other_indices():
    """The `include_open` branch skipped the `wanted` filter, so a NIFTY request also
    returned BANKNIFTY and SENSEX open bars — 3 'bars' at 23700/56400/75900."""
    rows = _cap_with_three_open().snapshot_minutes("NIFTY", include_open=True)
    assert [r["underlying"] for r in rows] == ["NIFTY"]


def test_snapshot_minutes_unfiltered_still_returns_all():
    rows = _cap_with_three_open().snapshot_minutes(None, include_open=True)
    assert {r["underlying"] for r in rows} == {"NIFTY", "BANKNIFTY", "SENSEX"}


def test_cross_index_bars_would_have_produced_the_observed_garbage():
    """Pins the exact production signature: 3 mixed-index bars -> RANGE conf 0.027
    with a physically impossible +220% intraday return."""
    mixed = _cap_with_three_open().snapshot_minutes(None, include_open=True)
    snap = classify_intraday(mixed)
    assert round(snap.confidence, 3) == 0.027
    assert snap.features["ret_pct"] > 100  # nonsense — that is the point


def test_bhavcopy_store_is_interned_per_root():
    """lru_cache on a METHOD keys on `self`; 45 construction sites each got their own
    copy of the cache and were pinned alive by it — the OOM multiplier."""
    assert BhavcopyStore() is BhavcopyStore()


def test_bhavcopy_row_cache_is_memory_budgeted():
    """Sized by rows (~2.4 KB each), not entry count: a NIFTY day is 15x a stock day,
    so a fixed maxsize cannot bound memory."""
    assert 0 < _ROWS_CACHE_BUDGET < 5_000_000
    store = BhavcopyStore()
    store.clear_caches()
    assert store.cache_stats()["rows"] == 0


def test_unbuildable_spread_reports_which_law_vetoed_it():
    """The selector discarded every candidate's reason and returned a bare
    'no valid dynamic spread candidates' — 279 of 360 signals died undiagnosable."""
    res = select_dynamic_credit_spread(
        chain_nodes=[], preferred_direction="bullish",
        width_points=200, lot_size=65, tp_frac=0.45, hold_minutes=300,
    )
    assert res["ok"] is False
    assert res["veto_law"]
    assert res["veto_counts"]
    assert len(res["vetoes"]) >= 1
    assert res["reason"] != "no valid dynamic spread candidates"


def test_overgated_book_is_not_muted_by_a_single_trade():
    """The 2026-07-24 shape exactly: 360 signals, 1 trade, 279/359 on one gate.
    `trades > 0` and a 0.8 dominance bar silenced it on both counts."""
    sigs = ([{"status": "PROCESSED"}]
            + [{"status": "SKIPPED_SIGNAL", "rejection_reason": "SPREAD_BUILD_FAILED"}] * 279
            + [{"status": "SKIPPED_SIGNAL", "rejection_reason": "RAE_ROUTER_STAND_DOWN"}] * 80)
    out = asyncio.run(overgated_book(
        ProbeContext(db=None, user_id="u", date_str="2026-07-24", signals_today=sigs)))
    assert len(out) == 1
    assert out[0].evidence["dominant_reason"] == "SPREAD_BUILD_FAILED"
    assert out[0].evidence["conversion_rate"] < 0.01


def test_overgated_book_stays_quiet_on_a_working_book():
    sigs = ([{"status": "PROCESSED"}] * 30
            + [{"status": "SKIPPED_SIGNAL", "rejection_reason": "X"}] * 30)
    assert asyncio.run(overgated_book(
        ProbeContext(db=None, user_id="u", date_str="d", signals_today=sigs))) == []


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, _n):
        return self._rows


class _FakeStarts:
    def __init__(self, rows):
        self._rows = rows

    def find(self, *_a, **_k):
        return _FakeCursor(self._rows)


class _FakeDb:
    def __init__(self, rows):
        self.app_starts = _FakeStarts(rows)


def test_process_restarts_flags_a_mid_session_restart_as_critical():
    """2026-07-24 restarted at 05:54 UTC (11:24 IST) with nothing recording it."""
    rows = [{"started_at": "2026-07-24T05:54:34+00:00"},
            {"started_at": "2026-07-24T15:04:14+00:00"}]
    out = asyncio.run(process_restarts(
        ProbeContext(db=_FakeDb(rows), user_id="u", date_str="2026-07-24")))
    assert len(out) == 1
    assert out[0].severity == "critical"
    assert out[0].evidence["restarts"] == 1


def test_process_restarts_quiet_on_a_single_clean_start():
    rows = [{"started_at": "2026-07-24T15:04:14+00:00"}]
    assert asyncio.run(process_restarts(
        ProbeContext(db=_FakeDb(rows), user_id="u", date_str="2026-07-24"))) == []
