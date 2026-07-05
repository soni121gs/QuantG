"""IMD-07 — intraday options backtest engine (deterministic, tiny fixtures)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.intraday_options_backtest import (  # noqa: E402
    EXIT_MISSING,
    EXIT_STOP,
    EXIT_SQUAREOFF,
    EXIT_TARGET,
    EXIT_TIME,
    IntradayCosts,
    run_day,
)

UND = "TESTX"  # unknown underlying -> lot_size 1, strike step 100 (clean arithmetic)
EXP = "2025-01-09"
CE_KEY = "CE24200|exp"


def _u(mins):
    return [{"timestamp_ist": f"2025-01-09T09:{15+k:02d}:00+05:30", "close": 24215.0} for k in range(mins)]


def _chain_at(_ts):
    return {EXP: {24200.0: {"CE": {"close": 100.0, "instrument_key": CE_KEY, "expired_instrument_key": CE_KEY}}}}


def _series(bars):
    return {CE_KEY: [{"timestamp_ist": f"2025-01-09T09:{15+k:02d}:00+05:30", **b} for k, b in enumerate(bars)]}


def _sig_first_bar(**over):
    def fn(history):
        if len(history) != 1:
            return None
        s = {"direction": "CE", "structure": "single_leg", "setup_type": "t",
             "target_R": 1.0, "initial_stop_R": 0.5, "max_hold_minutes": 999}
        s.update(over)
        return s
    return fn


def test_target_exit_exact_pnl():
    bars = [
        {"open": 100, "high": 100, "low": 100, "close": 100},  # 09:15 entry
        {"open": 140, "high": 150, "low": 95, "close": 140},   # 09:16
        {"open": 205, "high": 210, "low": 130, "close": 205},  # 09:17 -> target 200
    ]
    trades = run_day(underlying=UND, date=EXP, underlying_minutes=_u(3),
                     signal_fn=_sig_first_bar(), chain_at=_chain_at, option_series=_series(bars),
                     costs=IntradayCosts(slippage_pct=0.0, brokerage_per_leg=0.0))
    assert len(trades) == 1
    t = trades[0]
    assert t.exit_reason == EXIT_TARGET
    assert t.entry_price == 100.0 and t.exit_price == 200.0
    assert t.gross_pnl == 100.0 and t.net_pnl == 100.0
    assert t.r_multiple == 1.0
    assert t.mfe == 110.0 and t.mae == -5.0


def test_stop_beats_target_same_bar():
    bars = [
        {"open": 100, "high": 100, "low": 100, "close": 100},
        {"open": 100, "high": 250, "low": 40, "close": 100},  # low 40<=stop50 AND high 250>=target200 -> STOP wins
    ]
    trades = run_day(underlying=UND, date=EXP, underlying_minutes=_u(2),
                     signal_fn=_sig_first_bar(), chain_at=_chain_at, option_series=_series(bars),
                     costs=IntradayCosts(0.0, 0.0))
    assert trades[0].exit_reason == EXIT_STOP
    assert trades[0].exit_price == 50.0
    assert trades[0].gross_pnl == -50.0


def test_time_exit():
    bars = [
        {"open": 100, "high": 100, "low": 100, "close": 100},
        {"open": 110, "high": 115, "low": 90, "close": 110},
        {"open": 120, "high": 125, "low": 95, "close": 120},
    ]
    trades = run_day(underlying=UND, date=EXP, underlying_minutes=_u(3),
                     signal_fn=_sig_first_bar(max_hold_minutes=1), chain_at=_chain_at,
                     option_series=_series(bars), costs=IntradayCosts(0.0, 0.0))
    assert trades[0].exit_reason == EXIT_TIME
    assert trades[0].exit_price == 110.0  # closed at end of 1st held minute's close


def test_squareoff_at_last_bar():
    bars = [
        {"open": 100, "high": 100, "low": 100, "close": 100},
        {"open": 105, "high": 108, "low": 96, "close": 105},
    ]
    trades = run_day(underlying=UND, date=EXP, underlying_minutes=_u(2),
                     signal_fn=_sig_first_bar(), chain_at=_chain_at, option_series=_series(bars),
                     costs=IntradayCosts(0.0, 0.0))
    assert trades[0].exit_reason == EXIT_SQUAREOFF
    assert trades[0].exit_price == 105.0


def test_missing_price_fails_closed():
    # option series has no bar for the final minute -> forced exit at last known, flagged
    series = {CE_KEY: [
        {"timestamp_ist": "2025-01-09T09:15:00+05:30", "open": 100, "high": 100, "low": 100, "close": 100},
        {"timestamp_ist": "2025-01-09T09:16:00+05:30", "open": 120, "high": 122, "low": 98, "close": 120},
        # 09:17 missing
    ]}
    trades = run_day(underlying=UND, date=EXP, underlying_minutes=_u(3),
                     signal_fn=_sig_first_bar(), chain_at=_chain_at, option_series=series,
                     costs=IntradayCosts(0.0, 0.0))
    t = trades[0]
    assert t.exit_reason == EXIT_MISSING
    assert t.exit_price == 120.0
    assert any(f.startswith("missing_price") for f in t.data_quality_flags)


def test_costs_applied():
    bars = [
        {"open": 100, "high": 100, "low": 100, "close": 100},
        {"open": 205, "high": 210, "low": 130, "close": 205},  # target 200
    ]
    trades = run_day(underlying=UND, date=EXP, underlying_minutes=_u(2),
                     signal_fn=_sig_first_bar(), chain_at=_chain_at, option_series=_series(bars),
                     costs=IntradayCosts(slippage_pct=0.1, brokerage_per_leg=20.0))
    t = trades[0]
    # entry_fill 110, exit_fill 180, qty 1 -> gross 100, net (180-110)-40 = 30
    assert t.gross_pnl == 100.0
    assert t.net_pnl == 30.0


def test_no_trade_when_entry_price_missing():
    series = {CE_KEY: []}  # no entry bar
    trades = run_day(underlying=UND, date=EXP, underlying_minutes=_u(2),
                     signal_fn=_sig_first_bar(), chain_at=_chain_at, option_series=series,
                     costs=IntradayCosts(0.0, 0.0))
    assert trades == []
