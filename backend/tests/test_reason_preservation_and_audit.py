"""Regression guards for the 2026-07-29 session.

Two defects, both of which made a correctly-behaving book look broken:

1. `strategy_runner` overwrote the specific dispatch-boundary veto reason
   (cost floor / reachability / RAE router) with the generic "No current setup
   from strategy code" on its very next 15s tick. Every strategy in the book
   showed the useless text while the real diagnosis sat unread in db.signals.

2. A pause/resume left NO trace at all — no audit row, not even an updated_at
   bump — so a mid-session manual toggle was unreconstructable afterwards.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core.strategy_audit import (
    ACTOR_MANUAL,
    ACTOR_SCHEDULER,
    record_bulk_status_change,
    record_status_change,
)
from strategy_runner import SPECIFIC_REASON_TTL_SEC, _generic_reason

GENERIC = "No current setup from strategy code (candles=1351, source=upstox-v3-websocket)."
SPECIFIC = (
    "credit_spread not buildable (no valid dynamic spread candidates — 8 of 10 deltas "
    "vetoed by cost_floor (credit 33.10 on width 800 (ratio 0.041 < 0.120 min, "
    "achievable Rs331 < Rs900 floor))) — standing down"
)


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ── 1. reason preservation ────────────────────────────────────────────────


def test_fresh_specific_reason_survives_the_generic_runner_tick():
    out = _generic_reason(
        {"last_filter_reason": SPECIFIC, "last_filter_reason_at": _ago(30)}, GENERIC
    )
    assert "last_filter_reason" not in out, "runner clobbered a fresh veto reason"
    assert out["last_eval_note"] == GENERIC


def test_stale_specific_reason_is_replaced():
    out = _generic_reason(
        {"last_filter_reason": SPECIFIC, "last_filter_reason_at": _ago(SPECIFIC_REASON_TTL_SEC + 60)},
        GENERIC,
    )
    assert out["last_filter_reason"] == GENERIC


def test_no_prior_reason_writes_the_generic_text():
    assert _generic_reason({}, GENERIC)["last_filter_reason"] == GENERIC


def test_untimestamped_prior_reason_does_not_pin_forever():
    """A reason with no timestamp is pre-fix data — it must not win, or the
    generic text could never appear again."""
    out = _generic_reason({"last_filter_reason": SPECIFIC}, GENERIC)
    assert out["last_filter_reason"] == GENERIC


def test_future_timestamp_is_not_trusted():
    out = _generic_reason(
        {"last_filter_reason": SPECIFIC, "last_filter_reason_at": _ago(-3600)}, GENERIC
    )
    assert out["last_filter_reason"] == GENERIC


def test_garbage_timestamp_falls_back_to_generic():
    out = _generic_reason(
        {"last_filter_reason": SPECIFIC, "last_filter_reason_at": "not-a-date"}, GENERIC
    )
    assert out["last_filter_reason"] == GENERIC


# ── 2. status audit ───────────────────────────────────────────────────────


class _FakeCollection:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(doc)


class _FakeDB:
    def __init__(self):
        self.cols = {}

    def __getitem__(self, name):
        return self.cols.setdefault(name, _FakeCollection())


@pytest.mark.asyncio
async def test_manual_toggle_is_recorded_with_actor_and_source():
    db = _FakeDB()
    await record_status_change(
        db, user_id="u1", strategy_id="s1", name="RAE NIFTY Range Seller",
        old_status="live", new_status="paused", actor=ACTOR_MANUAL,
        source="PUT /strategies/{sid}/toggle",
    )
    (row,) = db["strategy_status_audit"].docs
    assert (row["old_status"], row["new_status"]) == ("live", "paused")
    assert row["actor"] == ACTOR_MANUAL
    assert row["strategy_name"] == "RAE NIFTY Range Seller"
    assert row["at_ist"].endswith("IST")


@pytest.mark.asyncio
async def test_noop_status_change_is_not_recorded():
    db = _FakeDB()
    await record_status_change(db, user_id="u1", strategy_id="s1",
                               old_status="live", new_status="live")
    assert db["strategy_status_audit"].docs == []


@pytest.mark.asyncio
async def test_scheduler_wake_distinguishable_from_a_manual_resume():
    db = _FakeDB()
    rows = [{"id": "a", "name": "A", "status": "paused"},
            {"id": "b", "name": "B", "status": "paused"},
            {"id": "c", "name": "C", "status": "live"}]  # already live → skipped
    written = await record_bulk_status_change(
        db, user_id="u1", rows=rows, new_status="live",
        actor=ACTOR_SCHEDULER, source="daily_scheduler 09:15 IST wake")
    assert written == 2
    assert {r["actor"] for r in db["strategy_status_audit"].docs} == {ACTOR_SCHEDULER}


@pytest.mark.asyncio
async def test_audit_failure_never_breaks_the_status_change():
    class _Boom:
        def __getitem__(self, name):
            raise RuntimeError("mongo down")

    await record_status_change(_Boom(), user_id="u1", strategy_id="s1",
                               old_status="live", new_status="paused")
