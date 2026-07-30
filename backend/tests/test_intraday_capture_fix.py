"""Intraday 1-minute capture reliability fix (2026-07-30).

Covers the two cleanly-unit-testable units of the fix:
  1. Hermes probe `infra.feed_down_at_open` — flags a morning the feed was down.
  2. Live option capture now accepts SENSEX refs (forward-only capture).

The EOD gap-backfill scheduler block shells out to the real importer CLIs and is
verified end-to-end on the VPS (a partial index day is re-fetched to a full session);
it is not unit-tested here because it is inline scheduler glue around a subprocess.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from core.hermes_diagnostics.probe_sdk import ProbeContext  # noqa: E402
from core.hermes_diagnostics.probes_infra import feed_down_at_open  # noqa: E402
from core.live_option_capture import LiveOptionCapture, CAPTURE_UNDERLYINGS  # noqa: E402


# ── fake async mongo ─────────────────────────────────────────────────────────
class _FakeColl:
    def __init__(self, docs):
        self._docs = docs

    async def find_one(self, query):
        for d in self._docs:
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None


class _FakeDB:
    def __init__(self, feed_open=None, flush=None):
        self.feed_open_status = _FakeColl(feed_open or [])
        self.capture_flush_runs = _FakeColl(flush or [])


def _ctx(db):
    return ProbeContext(db=db, user_id="u1", date_str="2026-07-30")


# ── infra.feed_down_at_open ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_feed_down_at_open_emits_critical():
    db = _FakeDB(
        feed_open=[{"date": "2026-07-30", "live": False, "reason": "no_token", "ts": "t"}],
        flush=[{"_id": "capture:2026-07-30", "index": {"bars": 219},
                "options": {"bars_written": 0}}],
    )
    out = await feed_down_at_open(_ctx(db))
    assert len(out) == 1
    f = out[0]
    assert f.probe_id == "infra.feed_down_at_open"
    assert str(f.severity).lower().endswith("critical")
    assert f.evidence["reason"] == "no_token"
    assert f.evidence["index_bars_captured"] == 219


@pytest.mark.asyncio
async def test_feed_live_at_open_is_silent():
    db = _FakeDB(feed_open=[{"date": "2026-07-30", "live": True, "reason": "ok"}])
    assert await feed_down_at_open(_ctx(db)) == []


@pytest.mark.asyncio
async def test_no_feed_status_doc_is_silent():
    assert await feed_down_at_open(_ctx(_FakeDB())) == []


# ── SENSEX live option capture ───────────────────────────────────────────────
def test_sensex_now_captured():
    assert "SENSEX" in CAPTURE_UNDERLYINGS
    cap = LiveOptionCapture()
    ref = cap._build_ref("SENSEX", {
        "instrument_key": "BSE_FO|123456", "strike": 77600,
        "option_type": "PE", "expiry": "2026-07-30",
    })
    assert ref is not None
    assert ref.underlying == "SENSEX"
    assert ref.instrument_key == "BSE_FO|123456"


def test_bankex_still_excluded():
    cap = LiveOptionCapture()
    ref = cap._build_ref("BANKEX", {
        "instrument_key": "BSE_FO|999", "strike": 60000,
        "option_type": "CE", "expiry": "2026-07-30",
    })
    assert ref is None
