"""Tests for the seller-capable intraday backtester (RES-8 IMD enablement).

The intraday judge was buyer-only (rejected negative entry_net). These prove the
new credit-spread path: a decaying spread PROFITS, a blowing-out spread STOPS,
and the selector builds the short+long legs.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.intraday_option_selector import select_contract
from core.intraday_options_backtest import run_day, IntradayCosts


def _chain():
    def node(key, close):
        return {"close": close, "instrument_key": key, "expired_instrument_key": key}
    return {"2025-01-02": {
        24700.0: {"PE": node("A", 40.0)},
        24650.0: {"PE": node("K1", 30.0)},   # short leg
        24600.0: {"PE": node("K2", 15.0)},   # long wing
    }}


def _mins(n=8):
    return [{"timestamp_ist": f"2025-01-02T09:{15 + i:02d}:00", "close": 24700.0,
             "high": 24700.0, "low": 24700.0} for i in range(n)]


def _series(k1_closes, k2_closes):
    def leg(closes):
        return [{"timestamp_ist": f"2025-01-02T09:{15 + i:02d}:00",
                 "close": c, "high": c, "low": c} for i, c in enumerate(closes)]
    return {"K1": leg(k1_closes), "K2": leg(k2_closes)}


def _sig(_hist):
    return {"direction": "PE", "structure": "credit_spread", "otm_offset_strikes": 1,
            "spread_width": 1, "credit_tp_frac": 0.35, "credit_sl_mult": 1.5,
            "max_hold_minutes": 60}


def test_selector_builds_credit_legs():
    sel = select_contract(underlying="NIFTY", spot=24700.0, chain=_chain(),
                          direction="PE", structure="credit_spread",
                          otm_offset_strikes=1, spread_width=1)
    assert sel.ok and sel.structure == "credit_spread" and len(sel.legs) == 2
    sides = {l.strike: l.side for l in sel.legs}
    assert sides[24650.0] == "SELL" and sides[24600.0] == "BUY"   # short near, long wing


def test_decaying_credit_spread_profits():
    # short 24650 PE decays 30→10, long 24600 PE 15→5 → credit shrinks → PROFIT.
    trades = run_day(underlying="NIFTY", date="2025-01-02", underlying_minutes=_mins(),
                     signal_fn=_sig, chain_at=lambda ts: _chain(),
                     option_series=_series([30, 30, 20, 12, 10, 10, 10, 10],
                                           [15, 15, 12, 8, 5, 5, 5, 5]),
                     costs=IntradayCosts(), max_trades=1)
    assert len(trades) == 1
    t = trades[0]
    assert t.structure == "credit_spread"
    assert t.net_pnl > 0                      # a decaying short spread makes money
    assert t.exit_reason in ("target", "time", "squareoff")


def test_blowout_credit_spread_stops_out():
    # short 24650 PE balloons 30→70 (spread runs against us) → STOP, a loss.
    trades = run_day(underlying="NIFTY", date="2025-01-02", underlying_minutes=_mins(),
                     signal_fn=_sig, chain_at=lambda ts: _chain(),
                     option_series=_series([30, 30, 70, 70, 70, 70, 70, 70],
                                           [15, 15, 18, 18, 18, 18, 18, 18]),
                     costs=IntradayCosts(), max_trades=1)
    assert len(trades) == 1
    assert trades[0].net_pnl < 0
    assert trades[0].exit_reason == "stop"
