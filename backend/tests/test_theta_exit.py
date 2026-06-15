"""Tests for Phase 2 #4 — theta-aware exits (core/position_lifecycle.py).

The theta-aware exit fires when a LONG option buyer stalls (no progress in R)
within a window that shrinks as theta burn steepens. It must:
  - fire on a stalled loser past the window,
  - hold a winner that has made progress,
  - fall back to a theta-free no-progress rule when greeks are absent,
  - skip shorts and equity,
  - shrink the window when theta is steep,
  - be a no-op when disabled.
"""
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.position_lifecycle as pl
from core.position_lifecycle import theta_aware_exit, exit_reason


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _opt_position(**overrides):
    pos = {
        "position_side": "LONG",
        "asset_type": "option",
        "option_type": "CE",
        "entry_time": _ago(10),
        "greeks_at_entry": {"theta": -30.0},
    }
    pos.update(overrides)
    return pos


# entry=100, stop=92 → risk_per_unit=8 → R = (ltp-100)/8


def test_fires_on_stalled_loser():
    pos = _opt_position()
    reason = theta_aware_exit(pos, ltp=99.0, entry=100.0, stop_price=92.0)
    assert reason is not None and reason.startswith("theta-decay")


def test_holds_a_winner():
    pos = _opt_position()
    # ltp=105 → progress_r = 0.625 > 0.3 → keep
    assert theta_aware_exit(pos, ltp=105.0, entry=100.0, stop_price=92.0) is None


def test_noprogress_fallback_without_theta():
    pos = _opt_position(greeks_at_entry={})  # no theta
    reason = theta_aware_exit(pos, ltp=99.0, entry=100.0, stop_price=92.0)
    assert reason is not None and reason.startswith("theta-no-progress")


def test_window_not_yet_elapsed():
    pos = _opt_position(entry_time=_ago(2))  # 2 min < floor/window
    assert theta_aware_exit(pos, ltp=99.0, entry=100.0, stop_price=92.0) is None


def test_steep_theta_shrinks_window():
    # theta=-60 → window ~5 min. Held 6 min → fires. theta-free → base window 8 → holds.
    steep = _opt_position(entry_time=_ago(6), greeks_at_entry={"theta": -60.0})
    assert theta_aware_exit(steep, ltp=99.0, entry=100.0, stop_price=92.0) is not None
    no_theta = _opt_position(entry_time=_ago(6), greeks_at_entry={})
    assert theta_aware_exit(no_theta, ltp=99.0, entry=100.0, stop_price=92.0) is None


def test_skips_short_option():
    pos = _opt_position(position_side="SHORT")
    assert theta_aware_exit(pos, ltp=99.0, entry=100.0, stop_price=92.0) is None


def test_skips_equity():
    pos = _opt_position(asset_type="equity", option_type=None, exchange="NSE")
    assert theta_aware_exit(pos, ltp=99.0, entry=100.0, stop_price=92.0) is None


def test_disabled_flag_is_noop(monkeypatch):
    monkeypatch.setattr(pl, "EXIT_THETA_AWARE_ENABLED", False)
    pos = _opt_position()
    assert theta_aware_exit(pos, ltp=99.0, entry=100.0, stop_price=92.0) is None


def test_exit_reason_integration_returns_theta_before_blind_timer():
    # Full position: SL=92, TP=114, ltp=99 (no SL/TP), held 10m, theta present,
    # time_exit_minutes=22 (not yet due) → theta-aware exit should fire first.
    pos = {
        "position_side": "LONG",
        "asset_type": "option",
        "option_type": "CE",
        "average_buy_price": 100.0,
        "entry_time": _ago(10),
        "greeks_at_entry": {"theta": -30.0},
        "tp_sl_tsl_config": {
            "stop_loss_pct": 8.0,
            "take_profit_pct": 14.0,
            "time_exit_minutes": 22,
            "adaptive_exits_enabled": False,
        },
    }
    reason = exit_reason(pos, ltp=99.0)
    assert reason is not None and reason.startswith("theta-")


def test_exit_reason_still_prefers_stop_loss():
    # ltp below stop → SL must win over theta exit.
    pos = {
        "position_side": "LONG",
        "asset_type": "option",
        "option_type": "CE",
        "average_buy_price": 100.0,
        "entry_time": _ago(10),
        "greeks_at_entry": {"theta": -30.0},
        "tp_sl_tsl_config": {
            "stop_loss_pct": 8.0,
            "take_profit_pct": 14.0,
            "time_exit_minutes": 22,
            "adaptive_exits_enabled": False,
        },
    }
    reason = exit_reason(pos, ltp=90.0)  # below 92 stop
    assert reason in ("stop-loss", "trailing-sl")
