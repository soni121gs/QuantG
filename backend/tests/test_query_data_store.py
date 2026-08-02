"""P5-K6 — the bounded, read-only, deterministic bhavcopy-store query tool.

Uses a fake store so the test is host-independent (no real data needed).
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import core.bhavcopy_store as bstore  # noqa: E402
from routes.ai import _query_data_store_sync, _QDS_MAX_STRIKES  # noqa: E402


class _FakeStore:
    _days = [f"2025-01-{d:02d}" for d in range(1, 21)]

    def trading_days(self, start=None, end=None):
        lo = start or self._days[0]
        hi = end or self._days[-1]
        return [d for d in self._days if lo <= d <= hi]

    def load_day(self, day):
        return [{"underlying": "NIFTY"}, {"underlying": "RELIANCE"}, {"underlying": "BANKNIFTY"}]

    def underlying_daily(self, underlying, start=None, end=None):
        return [{"date": f"{d} 11:00", "open": 100.0, "high": 101.0,
                 "low": 99.0, "close": 100.0 + i}
                for i, d in enumerate(self.trading_days(start, end))]

    def option_chain(self, underlying, day):
        return {"2025-01-30": {float(k): {"CE": {"close": 10.0}, "PE": {"close": 8.0}}
                               for k in range(20000, 20000 + 60 * 50, 50)}}


def _patch(monkeypatch):
    monkeypatch.setattr(bstore, "BhavcopyStore", _FakeStore)


def test_coverage_verb(monkeypatch):
    _patch(monkeypatch)
    r = _query_data_store_sync("how many trading days do we have")
    assert r["verb"] == "coverage"
    assert r["trading_days"] == 20
    assert "NIFTY" in r["underlyings_sample"]


def test_daily_verb_computes_realized_vol(monkeypatch):
    _patch(monkeypatch)
    r = _query_data_store_sync("NIFTY daily close realized vol 2025-01-01 2025-01-20")
    assert r["verb"] == "daily" and r["underlying"] == "NIFTY"
    assert r["n_bars"] == 20
    assert r["realized_vol_pct_annualized"] is not None


def test_chain_verb_is_capped(monkeypatch):
    _patch(monkeypatch)
    r = _query_data_store_sync("NIFTY option chain strikes on 2025-01-15")
    assert r["verb"] == "chain"
    assert r["strikes_shown"] <= _QDS_MAX_STRIKES
    assert r["chain"][0]["CE_close"] == 10.0


def test_longest_underlying_wins(monkeypatch):
    _patch(monkeypatch)
    r = _query_data_store_sync("show me BANKNIFTY daily close")
    assert r["underlying"] == "BANKNIFTY"   # not the 'NIFTY' substring


def test_empty_store_degrades_gracefully(monkeypatch):
    class _Empty(_FakeStore):
        def trading_days(self, start=None, end=None):
            return []
    monkeypatch.setattr(bstore, "BhavcopyStore", _Empty)
    r = _query_data_store_sync("coverage")
    assert r["verb"] == "coverage" and r.get("error")
