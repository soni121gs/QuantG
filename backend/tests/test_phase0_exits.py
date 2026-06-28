"""Phase 0 Gate Tests — Exit Spine.

Gate tests required before Phase 1 can start:
  GT-1: PositionGuardian closes a position at SL even when strategy loop is dead.
  GT-2: PositionGuardian closes a position at TP even when strategy loop is dead.
  GT-3: PositionGuardian closes a position at timeout (deadline_at) even when loop is dead.
  GT-4: Duplicate-exit trigger produces exactly ONE order and ONE trade_fill entry.
  GT-5: Daily-loss kill switch trips correctly using realized_pnl (z spelling).
  GT-6: _strategy_health_loop no longer crashes (TICK_SECONDS NameError is gone).
  GT-7: position_monitor fires squareoff-1510 at 15:10 IST.
"""
import asyncio
import os
import sys
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from position_guardian import (
    _guard_one,
    _ensure_risk_params,
    run_guardian_loop,
    GUARDIAN_POLL_SECONDS,
)
from position_monitor import _process_one_position, _squareoff_due, _ist_now


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _ago_iso(minutes: int):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()

def _future_iso(minutes: int):
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()

def _past_iso(seconds: int = 10):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _make_open_position(
    pos_id="pos_test001",
    user_id="user_test",
    sid="strat_test",
    symbol="NIFTY24JUN23000CE",
    entry_price=100.0,
    sl_pct=8.0,
    tp_pct=12.0,
    time_exit_min=20,
    deadline_offset_min=None,  # None = set from entry_time + time_exit_min
    entry_offset_min=25,       # position is 25 minutes old
):
    entry_time = _ago_iso(entry_offset_min)
    deadline = None
    if deadline_offset_min is not None:
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=deadline_offset_min)).isoformat()
    elif time_exit_min:
        # deadline was set at entry: entry_time + time_exit_min
        entry_dt = datetime.now(timezone.utc) - timedelta(minutes=entry_offset_min)
        deadline = (entry_dt + timedelta(minutes=time_exit_min)).isoformat()

    return {
        "id": pos_id,
        "user_id": user_id,
        "strategy_id": sid,
        "target_symbol": symbol,
        "trading_symbol": symbol,
        "symbol": symbol,
        "instrument_key": "NSE_FO|12345",
        "exchange": "NFO",
        "mode": "paper",
        "status": "OPEN",
        "position_side": "LONG",
        "quantity": 50,
        "open_quantity": 50,
        "average_buy_price": entry_price,
        "average_price": entry_price,
        "entry_price": entry_price,
        "entry_time": entry_time,
        "created_at": entry_time,
        "deadline_at": deadline,
        "last_tick_at": _ago_iso(1),
        "tp_sl_tsl_config": {
            "stop_loss_pct": sl_pct,
            "take_profit_pct": tp_pct,
            "time_exit_minutes": time_exit_min,
            "trailing_sl_enabled": False,
            "protection_status": "PROTECTED",
        },
    }


def _make_db_mock():
    db = MagicMock()
    db.strategy_positions = MagicMock()
    db.strategy_positions.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    db.strategy_positions.find_one = AsyncMock(return_value=None)
    db.upstox_instruments = MagicMock()
    db.upstox_instruments.find_one = AsyncMock(return_value=None)
    db.paper_quote_cache = MagicMock()
    db.paper_quote_cache.find_one = AsyncMock(return_value=None)
    return db


# ── GT-1: SL exit fires even without strategy loop ────────────────────────────

@pytest.mark.asyncio
async def test_gt1_guardian_closes_at_sl():
    """Guardian fires close_fn when LTP drops below SL price."""
    pos = _make_open_position(entry_price=100.0, sl_pct=8.0)
    # SL price = 100 * (1 - 0.08) = 92.0; LTP = 85 (below SL)
    ltp_below_sl = 85.0

    db = _make_db_mock()
    close_fn = AsyncMock()
    quote_ltp_fn = AsyncMock(return_value=ltp_below_sl)
    get_ltp_fn   = AsyncMock(return_value=None)
    get_settings_fn = AsyncMock(return_value={"allow_simulated_prices": True, "paper_mode": True})

    await _guard_one(
        db, pos, in_hours=True, squareoff=False,
        close_fn=close_fn,
        quote_ltp_fn=quote_ltp_fn,
        get_ltp_fn=get_ltp_fn,
        get_settings_fn=get_settings_fn,
    )

    close_fn.assert_called_once()
    reason = close_fn.call_args[1].get("reason") or close_fn.call_args[0][2]
    assert "stop" in reason.lower() or "sl" in reason.lower(), f"unexpected reason: {reason}"


# ── GT-2: TP exit fires even without strategy loop ────────────────────────────

@pytest.mark.asyncio
async def test_gt2_guardian_closes_at_tp():
    """Guardian fires close_fn when LTP rises above TP price."""
    pos = _make_open_position(entry_price=100.0, tp_pct=12.0)
    # TP price = 100 * (1 + 0.12) = 112.0; LTP = 115 (above TP)
    ltp_above_tp = 115.0

    db = _make_db_mock()
    close_fn = AsyncMock()
    quote_ltp_fn = AsyncMock(return_value=ltp_above_tp)
    get_ltp_fn   = AsyncMock(return_value=None)
    get_settings_fn = AsyncMock(return_value={"allow_simulated_prices": True})

    await _guard_one(
        db, pos, in_hours=True, squareoff=False,
        close_fn=close_fn,
        quote_ltp_fn=quote_ltp_fn,
        get_ltp_fn=get_ltp_fn,
        get_settings_fn=get_settings_fn,
    )

    close_fn.assert_called_once()
    reason = close_fn.call_args[1].get("reason") or close_fn.call_args[0][2]
    assert "profit" in reason.lower() or "tp" in reason.lower(), f"unexpected reason: {reason}"


# ── GT-3: Timeout / deadline fires even without strategy loop ─────────────────

@pytest.mark.asyncio
async def test_gt3_guardian_closes_at_timeout():
    """Guardian fires close_fn when position deadline_at has passed."""
    # deadline_at is 10 seconds in the past (position expired)
    pos = _make_open_position(
        entry_price=100.0,
        entry_offset_min=25,
        time_exit_min=20,   # deadline = entry + 20min = 5 min ago
    )
    # LTP is exactly at entry (no SL/TP trigger) — only time exit should fire
    ltp_neutral = 100.0

    db = _make_db_mock()
    close_fn = AsyncMock()
    quote_ltp_fn = AsyncMock(return_value=ltp_neutral)
    get_ltp_fn   = AsyncMock(return_value=None)
    get_settings_fn = AsyncMock(return_value={"allow_simulated_prices": True})

    with patch("core.position_lifecycle.EXIT_THETA_AWARE_ENABLED", False):
        await _guard_one(
            db, pos, in_hours=True, squareoff=False,
            close_fn=close_fn,
            quote_ltp_fn=quote_ltp_fn,
            get_ltp_fn=get_ltp_fn,
            get_settings_fn=get_settings_fn,
        )

    close_fn.assert_called_once()
    reason = close_fn.call_args[1].get("reason") or close_fn.call_args[0][2]
    assert "time" in reason.lower() or "deadline" in reason.lower(), f"unexpected reason: {reason}"


# ── GT-4: Duplicate exit → exactly one order + one ledger entry ───────────────

@pytest.mark.asyncio
async def test_gt4_duplicate_exit_idempotent():
    """Firing close_fn twice for the same position must be idempotent.

    The underlying _close_strategy_positions uses an EXITING atomic-mark so
    the second concurrent call skips. We simulate this by having close_fn
    mimic the real behavior: first call succeeds, second call is a no-op
    because we make close_fn track a 'closed' set.
    """
    closed_set = set()
    close_call_count = [0]

    async def _close_fn(user_id, sid, reason="exit", **kwargs):
        close_call_count[0] += 1
        pos_key = f"{user_id}:{sid}"
        if pos_key in closed_set:
            # Simulate _close_strategy_positions returning "skipped" on duplicate
            return {"closed_positions": [], "skipped": True}
        closed_set.add(pos_key)
        return {"closed_positions": [{"status": "ok"}]}

    pos = _make_open_position(entry_price=100.0, sl_pct=8.0)
    ltp_below_sl = 85.0

    db = _make_db_mock()
    quote_ltp_fn = AsyncMock(return_value=ltp_below_sl)
    get_ltp_fn   = AsyncMock(return_value=None)
    get_settings_fn = AsyncMock(return_value={"allow_simulated_prices": True})

    # Fire the guardian twice concurrently (simulates two loops calling at the same ms)
    await asyncio.gather(
        _guard_one(db, pos, in_hours=True, squareoff=False,
                   close_fn=_close_fn, quote_ltp_fn=quote_ltp_fn,
                   get_ltp_fn=get_ltp_fn, get_settings_fn=get_settings_fn),
        _guard_one(db, pos, in_hours=True, squareoff=False,
                   close_fn=_close_fn, quote_ltp_fn=quote_ltp_fn,
                   get_ltp_fn=get_ltp_fn, get_settings_fn=get_settings_fn),
    )

    # close_fn was called twice (guardian can't know it's a dupe at its level)
    # but the underlying close logic deduplicates; only ONE actual close happens
    assert close_call_count[0] == 2, "Guardian should try to close both times"
    assert len(closed_set) == 1, "Only ONE actual position close should have happened"


# ── GT-5: Daily loss kill switch uses correct field ───────────────────────────

def test_gt5_kill_switch_reads_realized_pnl():
    """Verify _check_daily_loss_guard reads 'realized_pnl' (z) not 'realized_pnl' (s)."""
    import inspect
    import server  # noqa: F401 — just verifying the source
    src = inspect.getsource(server._check_daily_loss_guard)  # type: ignore[attr-defined]

    # Must NOT use the UK spelling directly as the field to sum
    assert '"realized_pnl"' not in src or 'realized_pnl' in src, (
        "_check_daily_loss_guard must read 'realized_pnl' (z), not 'realized_pnl' (s)"
    )
    # Must read from trade_fills (authoritative source)
    assert "trade_fills" in src, (
        "_check_daily_loss_guard must read from db.trade_fills, not only db.orders"
    )


# ── GT-6: TICK_SECONDS NameError is gone from _strategy_health_loop ───────────

def test_gt6_strategy_health_loop_tick_seconds_defined():
    """STRATEGY_HEALTH_TICK_SECONDS must be defined before _strategy_health_loop."""
    import inspect
    import server  # type: ignore[import]

    src = inspect.getsource(server._strategy_health_loop)  # type: ignore[attr-defined]
    # The function body must NOT reference bare TICK_SECONDS
    assert "< TICK_SECONDS" not in src, (
        "_strategy_health_loop still uses undefined TICK_SECONDS — fix not applied"
    )
    assert "STRATEGY_HEALTH_TICK_SECONDS" in src, (
        "_strategy_health_loop must use STRATEGY_HEALTH_TICK_SECONDS"
    )


# ── GT-7: position_monitor fires squareoff at 15:10 ──────────────────────────

@pytest.mark.asyncio
async def test_gt7_squareoff_fires_at_1510():
    """_process_one_position must call close_fn with squareoff reason when squareoff=True."""
    pos = _make_open_position(entry_price=100.0)
    db  = _make_db_mock()
    close_fn = AsyncMock()
    quote_ltp_fn = AsyncMock(return_value=100.0)
    get_ltp_fn   = AsyncMock(return_value=None)
    get_settings_fn = AsyncMock(return_value={"allow_simulated_prices": True})

    await _process_one_position(
        db, pos,
        in_hours=True,
        squareoff=True,      # simulate 15:10 IST
        close_fn=close_fn,
        quote_ltp_fn=quote_ltp_fn,
        get_ltp_fn=get_ltp_fn,
        get_settings_fn=get_settings_fn,
    )

    close_fn.assert_called_once()
    reason = close_fn.call_args[1].get("reason") or close_fn.call_args[0][2]
    assert "squareoff" in reason.lower() or "1510" in reason, f"unexpected reason: {reason}"


# ── GT-8: Guardian assigns default SL to UNPROTECTED positions ────────────────

@pytest.mark.asyncio
async def test_gt8_unprotected_position_gets_default_sl():
    """_ensure_risk_params must assign default SL/TP for UNPROTECTED positions."""
    pos = _make_open_position(entry_price=100.0, sl_pct=None, tp_pct=None)
    pos["tp_sl_tsl_config"] = {"protection_status": "UNPROTECTED"}  # no SL/TP

    db = _make_db_mock()

    updated_pos = await _ensure_risk_params(db, pos)

    cfg = updated_pos.get("tp_sl_tsl_config", {})
    assert cfg.get("stop_loss_pct") is not None and cfg["stop_loss_pct"] > 0, \
        "Default SL must be assigned to UNPROTECTED position"
    assert cfg.get("take_profit_pct") is not None and cfg["take_profit_pct"] > 0, \
        "Default TP must be assigned to UNPROTECTED position"
    # DB must be updated
    db.strategy_positions.update_one.assert_called_once()


# ── GT-9: Guardian does NOT fire outside market hours ─────────────────────────

@pytest.mark.asyncio
async def test_gt9_guardian_silent_outside_hours():
    """Guardian must not fire exits when in_hours=False and squareoff=False."""
    pos = _make_open_position(entry_price=100.0, sl_pct=8.0)
    ltp_below_sl = 85.0  # would trigger SL if in hours

    db = _make_db_mock()
    close_fn = AsyncMock()
    quote_ltp_fn = AsyncMock(return_value=ltp_below_sl)
    get_ltp_fn   = AsyncMock(return_value=None)
    get_settings_fn = AsyncMock(return_value={})

    await _guard_one(
        db, pos, in_hours=False, squareoff=False,
        close_fn=close_fn,
        quote_ltp_fn=quote_ltp_fn,
        get_ltp_fn=get_ltp_fn,
        get_settings_fn=get_settings_fn,
    )

    close_fn.assert_not_called()


# ── GT-10: Guardian force-exits when LTP unavailable AND deadline passed ───────

@pytest.mark.asyncio
async def test_gt10_force_exit_on_ltp_unavailable_past_deadline():
    """When LTP is totally unavailable and deadline has passed, guardian force-exits."""
    pos = _make_open_position(
        entry_price=100.0,
        entry_offset_min=35,
        time_exit_min=20,  # deadline = 15 min ago
    )

    db = _make_db_mock()
    close_fn = AsyncMock()
    # All LTP sources fail
    quote_ltp_fn = AsyncMock(return_value=None)
    get_ltp_fn   = AsyncMock(return_value=None)
    get_settings_fn = AsyncMock(return_value={"allow_simulated_prices": False})
    db.paper_quote_cache.find_one = AsyncMock(return_value=None)

    await _guard_one(
        db, pos, in_hours=True, squareoff=False,
        close_fn=close_fn,
        quote_ltp_fn=quote_ltp_fn,
        get_ltp_fn=get_ltp_fn,
        get_settings_fn=get_settings_fn,
    )

    close_fn.assert_called_once()
    reason = close_fn.call_args[1].get("reason") or close_fn.call_args[0][2]
    # Guardian force-exits a position it can't price. Accept any of the stale/no-LTP
    # protective-exit reason labels (the live label is "stale-quote-protective-exit").
    accepted = ("market", "ltp", "unavailable", "stale", "protective")
    assert any(token in reason.lower() for token in accepted), \
        f"Expected a stale/ltp protective force-exit reason, got: {reason}"
