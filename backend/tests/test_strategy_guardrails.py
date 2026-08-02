import os
import sys
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _AsyncCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    async def to_list(self, length=None):
        return self.rows

    def __aiter__(self):
        self._iter = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FindCollection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, *args, **kwargs):
        return _AsyncCursor(self.rows)


class _ScorecardDb:
    def __init__(self, trades, strategies):
        self.trades = _FindCollection(trades)
        self.strategies = _FindCollection(strategies)


class _FakeBhavcopyStore:
    def __init__(self):
        self.days = [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(80)]

    def underlying_daily(self, underlying, start=None, end=None):
        return [
            {"date": day, "open": 24000.0, "high": 24100.0, "low": 23900.0, "close": 24000.0}
            for day in self.days
        ]

    def expiries(self, underlying, day):
        entry = date.fromisoformat(day)
        return [(entry + timedelta(days=7)).isoformat()]

    def option_chain(self, underlying, day):
        expiry = self.expiries(underlying, day)[0]
        strikes = [23000.0, 23100.0, 23200.0, 23300.0, 23400.0, 24000.0, 24600.0, 24700.0]
        return {expiry: {strike: {} for strike in strikes}}

    def leg_settle(self, underlying, day, expiry, strike, option_type):
        if option_type == "PE":
            return 100.0 if strike >= 23300.0 else 40.0
        return 100.0 if strike <= 24700.0 else 40.0

    def leg_lot_size(self, underlying, day, expiry, strike, option_type):
        return 65


class _FakeCalendarStore(_FakeBhavcopyStore):
    def expiries(self, underlying, day):
        entry = date.fromisoformat(day)
        return [(entry + timedelta(days=7)).isoformat(), (entry + timedelta(days=35)).isoformat()]

    def option_chain(self, underlying, day):
        expiries = self.expiries(underlying, day)
        strikes = [24000.0]
        return {exp: {strike: {"CE": {}, "PE": {}} for strike in strikes} for exp in expiries}

    def leg_settle(self, underlying, day, expiry, strike, option_type):
        dte = (date.fromisoformat(expiry) - date.fromisoformat(day)).days
        intrinsic = 0.0
        return max(1.0, intrinsic + dte * 2.0)


def test_eod_backtester_replays_latest_window_strategy_across_history():
    from core.eod_options_backtest import EODOptionsBacktest

    strategy = {
        "id": "qg-latest",
        "name": "Latest Window Credit Spread",
        "python_code": "def run(data):\n    if len(data) < 20:\n        return []\n    d = data[-1]\n    return [{'date': d['date'], 'action': 'BUY', 'direction': 'CE'}]",
        "visual_config": {
            "symbol": "NIFTY",
            "options": {
                "enabled": True,
                "underlying": "NIFTY",
                "structure": "credit_spread",
                "spread_width": 2,
                "short_otm_pct": 0.03,
                "exit_mode": "expiry",
            },
            "risk": {"max_hold_days": 10},
        },
    }

    result = EODOptionsBacktest(_FakeBhavcopyStore()).run(strategy)

    assert result.get("error") is None
    assert result["signal_evaluation"] == "rolling_latest_window"
    assert result["signals"] > 1
    assert result["total_trades"] > 0


def test_eod_backtester_prices_calendar_spread_two_expiries():
    from core.eod_options_backtest import EODOptionsBacktest

    strategy = {
        "id": "qg-calendar",
        "name": "Calendar Spread Research",
        "python_code": "def run(data):\n    return []",
        "visual_config": {
            "symbol": "NIFTY",
            "options": {
                "enabled": True,
                "underlying": "NIFTY",
                "structure": "calendar_spread",
                "exit_mode": "expiry",
            },
            "risk": {"max_hold_days": 10},
        },
    }

    result = EODOptionsBacktest(_FakeCalendarStore()).run(
        strategy,
        params={"calendar_far_min_dte": 21, "calendar_far_max_dte": 60},
        signals=[{"date": "2026-01-21", "action": "BUY", "direction": "CE"}],
    )

    assert result.get("error") is None
    assert result["structure"] == "calendar_spread"
    assert result["total_trades"] == 1
    trade = result["trades"][0]
    assert "CAL" in trade["desc"]
    assert trade["exit_reason"] == "EXPIRY"


@pytest.mark.asyncio
async def test_archive_restore_and_toggle_keep_archived_strategies_out_of_live_flow():
    from routes.strategies import archive_strategy, restore_strategy, toggle_strategy

    db = MagicMock()
    db.strategies.find_one = AsyncMock(return_value={
        "id": "qg-old",
        "user_id": "user-1",
        "status": "live",
        "mode": "paper",
    })
    db.strategies.update_one = AsyncMock()

    with patch("routes.strategies.db", db):
        archived = await archive_strategy("qg-old", user={"id": "user-1"})

    update_fields = db.strategies.update_one.await_args.args[1]["$set"]
    assert archived["status"] == "archived"
    assert update_fields["status"] == "archived"
    assert update_fields["mode"] == "paper"
    assert update_fields["manual_paused"] is True

    db.strategies.update_one.reset_mock()
    with patch("routes.strategies.db", db), patch("core.market_clock.is_trading_session_active", return_value=False):
        restored = await restore_strategy("qg-old", user={"id": "user-1"})

    update_fields = db.strategies.update_one.await_args.args[1]["$set"]
    assert restored == {"ok": True, "status": "paused", "schedule_paused": True}
    assert update_fields["status"] == "paused"
    assert update_fields["mode"] == "paper"
    assert update_fields["schedule_paused"] is True

    db.strategies.find_one = AsyncMock(return_value={"id": "qg-old", "user_id": "user-1", "status": "archived"})
    with patch("routes.strategies.db", db):
        with pytest.raises(HTTPException) as exc:
            await toggle_strategy("qg-old", user={"id": "user-1"})
    assert exc.value.status_code == 400
    assert "archived" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_options_strategy_test_run_is_oos_read_only_and_never_places_orders():
    from routes.strategies import test_run_strategy

    db = MagicMock()
    db.strategies.find_one = AsyncMock(return_value={
        "id": "qg-o1",
        "user_id": "user-1",
        "name": "QG-O1 NIFTY Put Spread Theta Core",
        "python_code": "def run(data):\n    return [{'action': 'BUY'}]",
        "visual_config": {"options": {"enabled": True, "underlying": "NIFTY"}},
        "mode": "paper",
        "status": "paused",
    })
    backtest = {
        "results": [{
            "verdict": "INSUFFICIENT_DATA",
            "oos_year": 2025,
            "oos": {"expectancy": -12.5},
            "pct_green_months": 42.0,
            "by_year": [{"year": 2025, "pnl": -250}],
            "overall": {"n": 10, "win_rate": 60.0, "pnl": 500.0, "expectancy": 50.0},
        }]
    }

    with patch("routes.strategies.db", db), \
         patch("routes.ops.ops_eod_options_backtest", new=AsyncMock(return_value=backtest)), \
         patch("server._place_order_core", new=AsyncMock(side_effect=AssertionError("must not place order"))) as place_order, \
         patch("server._resolve_option_for_strategy", new=AsyncMock(side_effect=AssertionError("must not resolve contract"))):
        result = await test_run_strategy("qg-o1", user={"id": "user-1"})

    assert result["ok"] is True
    assert result["engine"] == "eod_options_oos"
    assert result["oos_backtest"] is True
    assert result["data_live"] is False
    assert result["summary"]["trades"] == 10
    assert result["summary"]["wins"] == 6
    place_order.assert_not_awaited()


def test_edge_lab_scores_only_active_book_and_reports_archived_count(monkeypatch):
    from core import edge_lab

    active = {"id": "active", "name": "QG-O1", "status": "paused", "visual_config": {"options": {"underlying": "NIFTY"}}}
    archived = {"id": "archived", "name": "Old Strategy", "status": "archived", "visual_config": {"options": {"underlying": "NIFTY"}}}
    seen = {}

    class Store:
        def trading_days(self):
            return ["2026-01-01"]

    monkeypatch.setattr(edge_lab, "_coverage", lambda store, days: {"underlyings": [{"underlying": "NIFTY"}]})
    monkeypatch.setattr(edge_lab, "_iv_surfaces", lambda store, days, present: {"samples": []})
    monkeypatch.setattr(edge_lab, "_base_rate", lambda store: [])
    monkeypatch.setattr(edge_lab, "_directional", lambda store: [])

    def fake_oos(strategies, store, present):
        seen["oos"] = strategies
        return {"rows": [{"name": s["name"], "verdict": "INSUFFICIENT_DATA"} for s in strategies], "counts": {"INSUFFICIENT_DATA": len(strategies)}}

    monkeypatch.setattr(edge_lab, "_oos", fake_oos)

    snapshot = edge_lab.build_snapshot([active, archived], store=Store(), include_sweep=False)

    assert seen["oos"] == [active]
    assert snapshot["book"] == {"active_strategies": 1, "archived_strategies": 1}
    assert [row["name"] for row in snapshot["oos"]["rows"]] == ["QG-O1"]


def test_judge_facade_event_mode_filters_to_event_window():
    from core.judge_facade import grade

    strategy = {
        "id": "qg-event",
        "name": "Event Window Credit Spread",
        "python_code": "def run(data):\n    return [{'date': d['date'], 'action': 'BUY', 'direction': 'CE'} for d in data]",
        "visual_config": {
            "symbol": "NIFTY",
            "options": {
                "enabled": True,
                "underlying": "NIFTY",
                "structure": "credit_spread",
                "spread_width": 2,
                "short_otm_pct": 0.03,
                "exit_mode": "expiry",
            },
            "risk": {"max_hold_days": 10},
        },
    }

    result = grade(
        strategy,
        mode="event",
        store=_FakeBhavcopyStore(),
        params={"event_dates": ["2026-01-20"], "event_window_days": 1},
    )

    assert result["status"] == "ready"
    assert result["mode"] == "event"
    assert result["event_filter"]["enabled"] is True
    assert result["event_filter"]["signals_before"] > result["event_filter"]["signals_after"]


@pytest.mark.asyncio
async def test_scorecard_include_empty_shows_active_strategies_but_not_archive():
    from core.strategy_scorecard import build_scorecard

    db = _ScorecardDb(
        trades=[],
        strategies=[
            {"id": "active", "user_id": "user-1", "name": "QG-O1", "status": "paused", "visual_config": {"options": {"underlying": "NIFTY"}}},
            {"id": "archived", "user_id": "user-1", "name": "Old Dead", "status": "archived", "visual_config": {"options": {"underlying": "NIFTY"}}},
        ],
    )

    rows = await build_scorecard(db, user_id="user-1", mode="paper", include_empty=True)

    assert [row["name"] for row in rows] == ["QG-O1"]
    assert rows[0]["verdict"] == "WATCH"
    assert rows[0]["verdict_reason"] == "awaiting realized paper trades"


# --- P5-J1 (2026-07-23): the judge now weighs variance, not just expectancy ---

def test_bucket_metrics_reports_tstat_and_handles_edge_cases():
    from core.eod_options_backtest import _bucket_metrics
    tight = _bucket_metrics([{"pnl": p} for p in [500, 480, 520, 510, 490] * 8])
    assert tight["t_stat"] > 3 and tight["std"] > 0
    noisy = _bucket_metrics([{"pnl": p} for p in [3000, -2900, 3100, -2800, 50] * 8])
    assert abs(noisy["t_stat"]) < 3          # same-ish mean, huge variance -> insignificant
    assert _bucket_metrics([])["t_stat"] is None
    assert _bucket_metrics([{"pnl": 100}])["t_stat"] is None   # n=1, no dispersion


def test_walk_forward_rejects_high_variance_positive_expectancy():
    """A positive expectancy that is statistically indistinguishable from zero must
    be FRAGILE, never CANDIDATE_EDGE — the fix for the small-sample illusion."""
    from core.eod_options_backtest import walk_forward

    def _mk(pnls):
        out = []
        for i, p in enumerate(pnls):
            y = 2024 if i < len(pnls) // 2 else 2025
            out.append({"pnl": p, "exit_date": f"{y}-{(i % 12) + 1:02d}-15"})
        return out

    noisy = walk_forward({"trades": _mk([2000, -1900, 2100, -1800, 50] * 10)})
    assert noisy["overall"]["expectancy"] > 0        # positive on average
    assert noisy["overall"]["t_stat"] < 3
    assert noisy["verdict"] == "FRAGILE"

    tight = walk_forward({"trades": _mk([200, 180, 220, 210, 190] * 10)})
    assert tight["verdict"] == "CANDIDATE_EDGE"       # low variance, persistent -> real


def test_hac_se_inflates_error_for_autocorrelated_returns():
    """P5-J5: option P&L is positively autocorrelated (vol clusters, holds overlap),
    so the iid SE understates the true error. The Newey-West/HAC t-stat must be
    smaller (less significant) than the iid one on a positively autocorrelated
    series — otherwise the judge flatters clustered noise."""
    from core.eod_options_backtest import _bucket_metrics, _hac_se
    # long positively-autocorrelated run: two regimes back to back
    series = [100] * 30 + [140] * 30      # slow drift => strong positive autocorr
    m = _bucket_metrics([{"pnl": p} for p in series])
    assert m["t_stat_hac"] is not None and m["t_stat"] is not None
    assert abs(m["t_stat_hac"]) < abs(m["t_stat"])   # HAC is the conservative one
    # HAC SE strictly exceeds the iid SE on this series
    mean = sum(series) / len(series)
    sd = (sum((p - mean) ** 2 for p in series) / (len(series) - 1)) ** 0.5
    assert _hac_se(series, mean) > sd / len(series) ** 0.5
    # an i.i.d.-looking alternating series should NOT be inflated much
    assert _hac_se([100, -100] * 30, 0.0) is not None
