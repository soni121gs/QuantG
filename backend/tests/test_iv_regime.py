"""Tests for Phase 2 #1 — IV-rank regime gate (iv_regime.py)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import iv_regime
from iv_regime import compute_iv_rank, iv_buy_gate


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def sort(self, *a, **k):
        return self

    async def to_list(self, n):
        return self._rows[:n]


class _FakeColl:
    def __init__(self, rows):
        self._rows = rows

    def find(self, *a, **k):
        return _FakeCursor(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self.vix_history = _FakeColl(rows)


def _rows(values):
    # Caller passes values latest-first (matches date-desc sort).
    return [{"date": f"d{i}", "value": v} for i, v in enumerate(values)]


def _fresh():
    iv_regime._cache.update({"at": None, "data": None})


def test_iv_rank_math():
    _fresh()
    # latest=14, min=10, max=28 → rank = (14-10)/(28-10)*100 = 22.2
    db = _FakeDB(_rows([14.0, 10.0, 28.0, 20.0]))
    data = asyncio.run(compute_iv_rank(db))
    assert data["current"] == 14.0 and data["min"] == 10.0 and data["max"] == 28.0
    assert abs(data["iv_rank"] - 22.2) < 0.2
    assert data["n"] == 4


def test_iv_rank_none_on_insufficient_history():
    _fresh()
    assert asyncio.run(compute_iv_rank(_FakeDB(_rows([14.0])))) is None
    _fresh()
    assert asyncio.run(compute_iv_rank(_FakeDB([]))) is None


def test_gate_passes_below_cap(monkeypatch):
    monkeypatch.setattr(iv_regime, "IV_RANK_BUY_MAX", 70.0)
    monkeypatch.setattr(iv_regime, "IV_RANK_GATE_ENABLED", True)
    d = iv_buy_gate(40.0)
    assert d["block"] is False and d["shadow"] is False


def test_gate_blocks_above_cap_when_enabled(monkeypatch):
    monkeypatch.setattr(iv_regime, "IV_RANK_BUY_MAX", 70.0)
    monkeypatch.setattr(iv_regime, "IV_RANK_GATE_ENABLED", True)
    monkeypatch.setattr(iv_regime, "IV_RANK_GATE_SHADOW", False)
    d = iv_buy_gate(85.0)
    assert d["block"] is True and "IV_RANK_HIGH" in d["reason"]


def test_gate_shadow_does_not_block(monkeypatch):
    monkeypatch.setattr(iv_regime, "IV_RANK_BUY_MAX", 70.0)
    monkeypatch.setattr(iv_regime, "IV_RANK_GATE_ENABLED", False)
    monkeypatch.setattr(iv_regime, "IV_RANK_GATE_SHADOW", True)
    d = iv_buy_gate(85.0)
    assert d["block"] is False and d["shadow"] is True and d["reason"] is not None


def test_gate_off_is_noop(monkeypatch):
    monkeypatch.setattr(iv_regime, "IV_RANK_BUY_MAX", 70.0)
    monkeypatch.setattr(iv_regime, "IV_RANK_GATE_ENABLED", False)
    monkeypatch.setattr(iv_regime, "IV_RANK_GATE_SHADOW", False)
    d = iv_buy_gate(85.0)
    assert d["block"] is False and d["shadow"] is False


def test_gate_none_rank_passes():
    assert iv_buy_gate(None)["block"] is False
