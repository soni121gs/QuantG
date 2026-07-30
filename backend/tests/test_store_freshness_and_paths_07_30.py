"""2026-07-30 — the judges were blind and the reason was one directory.

`data.store_coverage` had correctly reported bhavcopy_fo / index_1m / options_1m
as EMPTY for up to 13 days. It was right. What it could not say was WHY: 102 MB of
bhavcopy (2019-2023, 1,234 days) and 1,359 participant-OI files were sitting in
/opt/QuantG/backend/data/ while the container bind-mounts the repo-root ./data to
/app/data. "EMPTY" reads as a backfill chore, so nobody moved the files.

Second half of the lesson: restoring that store would have been WORSE than leaving
it empty, because its newest bar was 2023-12-29 and neither the realized-vol path
nor the IV-surface path checked how old the data was. An empty store fails open and
says so; a stale one is confidently wrong (§22.7's stale-regime trap again).
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from core import entry_gate


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# ── freshness guard ───────────────────────────────────────────────────────


def test_fresh_store_day_accepted():
    assert entry_gate._store_day_is_fresh(_iso(1))
    assert entry_gate._store_day_is_fresh(_iso(entry_gate._MAX_STORE_STALENESS_DAYS))


def test_stale_store_day_refused():
    assert not entry_gate._store_day_is_fresh(_iso(entry_gate._MAX_STORE_STALENESS_DAYS + 1))
    assert not entry_gate._store_day_is_fresh("2023-12-29"), "the exact live case"


def test_unparseable_day_is_not_fresh():
    assert not entry_gate._store_day_is_fresh("")
    assert not entry_gate._store_day_is_fresh("not-a-date")


def test_realized_vol_refuses_a_stale_store(monkeypatch):
    """The live failure: a store holding only 2019-2023 must yield NO closes, so
    the gate fails open instead of computing vol from 2.5-year-old prices."""
    class _StaleStore:
        def underlying_daily(self, _u):
            base = datetime(2023, 12, 1, tzinfo=timezone.utc).date()
            return [{"date": (base + timedelta(days=i)).isoformat(), "close": 20000 + i}
                    for i in range(25)]

    import core.bhavcopy_store as bs
    monkeypatch.setattr(bs, "BhavcopyStore", lambda *a, **k: _StaleStore())
    entry_gate._rv_cache.clear()
    try:
        assert entry_gate._recent_daily_closes("NIFTY") == []
    finally:
        entry_gate._rv_cache.clear()


def test_realized_vol_accepts_a_current_store(monkeypatch):
    """Guard the other direction — the fix must not starve a healthy store."""
    class _FreshStore:
        def underlying_daily(self, _u):
            today = datetime.now(timezone.utc).date()
            return [{"date": (today - timedelta(days=24 - i)).isoformat(),
                     "close": 24000 + i} for i in range(25)]

    import core.bhavcopy_store as bs
    monkeypatch.setattr(bs, "BhavcopyStore", lambda *a, **k: _FreshStore())
    entry_gate._rv_cache.clear()
    try:
        closes = entry_gate._recent_daily_closes("NIFTY")
        assert len(closes) >= 20
    finally:
        entry_gate._rv_cache.clear()


# ── path-mismatch probe ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_path_mismatch_probe_finds_data_one_directory_away(tmp_path, monkeypatch):
    from core.hermes_diagnostics import probes_data

    store_root = tmp_path / "data" / "bhavcopy_fo"
    store_root.mkdir(parents=True)                       # exists but EMPTY
    sibling = tmp_path / "backend" / "data" / "bhavcopy_fo" / "2019"
    sibling.mkdir(parents=True)
    for i in range(3):
        (sibling / f"BhavCopy_FO_2019010{i}.csv.gz").write_bytes(b"x")

    import core.bhavcopy_store as bs
    monkeypatch.setattr(bs, "STORE_ROOT", str(store_root))
    monkeypatch.setattr(probes_data, "_SIBLING_DATA_ROOTS",
                        (str(tmp_path / "backend" / "data"),))

    findings = await probes_data.store_path_mismatch(None)
    hits = [f for f in findings if f.entity == "bhavcopy_fo"]
    assert hits, "data sitting one directory away must be reported"
    assert hits[0].evidence["files_at_sibling"] == 3
    assert hits[0].evidence["files_in_store_root"] == 0
    assert "PATH problem" in hits[0].detail


@pytest.mark.asyncio
async def test_path_mismatch_probe_silent_when_store_is_populated(tmp_path, monkeypatch):
    from core.hermes_diagnostics import probes_data

    store_root = tmp_path / "data" / "bhavcopy_fo" / "2026"
    store_root.mkdir(parents=True)
    (store_root / "BhavCopy_FO_20260101.csv.gz").write_bytes(b"x")

    import core.bhavcopy_store as bs
    monkeypatch.setattr(bs, "STORE_ROOT", str(tmp_path / "data" / "bhavcopy_fo"))
    monkeypatch.setattr(probes_data, "_SIBLING_DATA_ROOTS", (str(tmp_path / "backend" / "data"),))

    findings = await probes_data.store_path_mismatch(None)
    assert [f for f in findings if f.entity == "bhavcopy_fo"] == []
