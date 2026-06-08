"""Tests for position_monitor._monitor_tick — specifically the EXITING revert logic.

The monitor runs every 30s and reverts positions stuck in EXITING > 5 minutes
back to OPEN so the exit can be retried. These tests verify that logic without
needing a running DB or event loop.
"""
import os
import sys
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from position_monitor import _monitor_tick, _in_market_hours


# ── Helpers ────────────────────────────────────────────────────────────────────

def _stale_ts(minutes_ago: int = 10) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _fresh_ts(minutes_ago: int = 2) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _make_db(stuck_positions=None, open_positions=None):
    """Return a mock db whose strategy_positions.find() responds correctly to
    both the EXITING query and the OPEN/FILLED/PENDING_BROKER query."""
    stuck_positions = stuck_positions or []
    open_positions = open_positions or []

    mock_db = MagicMock()

    def find_side_effect(query, *args, **kwargs):
        cursor = MagicMock()
        if isinstance(query.get("status"), str) and query["status"] == "EXITING":
            cursor.to_list = AsyncMock(return_value=stuck_positions)
        else:
            cursor.to_list = AsyncMock(return_value=open_positions)
        return cursor

    mock_db.strategy_positions.find = MagicMock(side_effect=find_side_effect)
    mock_db.strategy_positions.update_one = AsyncMock(
        return_value=MagicMock(modified_count=1)
    )
    return mock_db


def _noop_fns():
    return dict(
        close_fn=AsyncMock(),
        quote_ltp_fn=AsyncMock(return_value=None),
        get_ltp_fn=AsyncMock(return_value=None),
        get_settings_fn=AsyncMock(return_value={}),
    )


# ── EXITING revert tests ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_stuck_exiting_position_reverted_to_open():
    """A position stuck in EXITING for > 5 minutes must be set back to OPEN."""
    stuck = [{"id": "pos-stuck-1", "user_id": "user-1"}]
    db = _make_db(stuck_positions=stuck)

    await _monitor_tick(db, **_noop_fns())

    db.strategy_positions.update_one.assert_called_once()
    filter_arg, update_arg = db.strategy_positions.update_one.call_args.args
    assert filter_arg == {"id": "pos-stuck-1", "status": "EXITING"}
    assert update_arg["$set"]["status"] == "OPEN"


@pytest.mark.anyio
async def test_multiple_stuck_positions_all_reverted():
    """Every stuck EXITING position in the result set must be reverted."""
    stuck = [
        {"id": "pos-a", "user_id": "user-1"},
        {"id": "pos-b", "user_id": "user-2"},
        {"id": "pos-c", "user_id": "user-1"},
    ]
    db = _make_db(stuck_positions=stuck)

    await _monitor_tick(db, **_noop_fns())

    assert db.strategy_positions.update_one.call_count == 3
    reverted_ids = {
        c.args[0]["id"]
        for c in db.strategy_positions.update_one.call_args_list
    }
    assert reverted_ids == {"pos-a", "pos-b", "pos-c"}


@pytest.mark.anyio
async def test_no_stuck_positions_no_update():
    """When no positions are stuck in EXITING, update_one must not be called."""
    db = _make_db(stuck_positions=[])

    await _monitor_tick(db, **_noop_fns())

    db.strategy_positions.update_one.assert_not_called()


@pytest.mark.anyio
async def test_revert_sets_status_open_not_exiting():
    """The revert update must write status=OPEN, never leave it as EXITING."""
    stuck = [{"id": "pos-x", "user_id": "user-1"}]
    db = _make_db(stuck_positions=stuck)

    await _monitor_tick(db, **_noop_fns())

    _, update_arg = db.strategy_positions.update_one.call_args.args
    assert update_arg["$set"]["status"] == "OPEN"
    assert "updated_at" in update_arg["$set"]


@pytest.mark.anyio
async def test_revert_filter_guards_against_status_race():
    """The update_one filter must include status=EXITING so a concurrent
    transition (e.g. another process already set it to OPEN) is a no-op."""
    stuck = [{"id": "pos-race", "user_id": "user-1"}]
    db = _make_db(stuck_positions=stuck)

    await _monitor_tick(db, **_noop_fns())

    filter_arg, _ = db.strategy_positions.update_one.call_args.args
    assert filter_arg.get("status") == "EXITING", (
        "Filter must include status=EXITING to prevent overwriting a position "
        "that was already resolved by another coroutine"
    )


# ── Market hours helper ─────────────────────────────────────────────────────────

def test_in_market_hours_returns_bool():
    result = _in_market_hours()
    assert isinstance(result, bool)


def test_weekend_is_not_market_hours():
    # Saturday = weekday 5
    saturday = datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc)  # Sat 10:00 UTC → 15:30 IST
    with patch("position_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = saturday
        assert _in_market_hours() is False


def test_weekday_inside_window_is_market_hours():
    # Monday 04:00 UTC = 09:30 IST (inside 09:15–15:30)
    monday_in_hours = datetime(2026, 6, 8, 4, 0, tzinfo=timezone.utc)
    with patch("position_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = monday_in_hours
        assert _in_market_hours() is True


def test_weekday_outside_window_is_not_market_hours():
    # Monday 02:00 UTC = 07:30 IST (before 09:15 open)
    monday_before_open = datetime(2026, 6, 8, 2, 0, tzinfo=timezone.utc)
    with patch("position_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = monday_before_open
        assert _in_market_hours() is False
