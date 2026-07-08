"""Tests for RES-7 — core/seller_scalper.py (the strategy brain).

Verifies the assembly: RES-2/5 side gate, RES-4 cadence, RES-6 portfolio caps,
and the entry window — each blocker independently, plus the happy path.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.seller_scalper import decide, default_config


def _ctx(allow=True, side="PE", reasons=None):
    return {"seller_decision": {"allow_sell": allow, "side": side,
                                "reasons": reasons or ["all gates passed"]}}


def _book(**over):
    base = {
        "strategy_open_count": 0,
        "trades_today": 0,
        "seconds_since_last_exit": None,
        "open_positions": [],
        "new_spread_max_loss": 2000.0,
        "realized_today": 0.0,
        "minute_of_day": 600,   # 10:00 IST, inside the window
    }
    base.update(over)
    return base


def test_enter_happy_path_bull_put():
    d = decide(context=_ctx(side="PE"), book_state=_book())
    assert d["enter"] is True
    assert d["side"] == "PE" and d["direction"] == "bullish"
    assert d["structure"] == "credit_spread"
    assert d["credit_tp_frac"] == default_config()["credit_tp_frac"]


def test_enter_bear_call_when_side_ce():
    d = decide(context=_ctx(side="CE"), book_state=_book())
    assert d["enter"] is True and d["direction"] == "bearish"


def test_blocked_by_signal_gate():
    d = decide(context=_ctx(allow=False, side=None, reasons=["regime TREND_DOWN not sell-safe"]),
               book_state=_book())
    assert d["enter"] is False and d["blocked_stage"] == "signal"


def test_blocked_outside_entry_window():
    d = decide(context=_ctx(), book_state=_book(minute_of_day=9 * 60 + 30))  # 09:30
    assert d["enter"] is False and d["blocked_stage"] == "window"


def test_blocked_by_pyramiding():
    d = decide(context=_ctx(), book_state=_book(strategy_open_count=1))
    assert d["enter"] is False and d["blocked_stage"] == "cadence"
    assert "pyramiding" in d["reason"]


def test_blocked_by_daily_cap():
    d = decide(context=_ctx(), book_state=_book(trades_today=6))
    assert d["enter"] is False and d["blocked_stage"] == "cadence"


def test_blocked_by_cooldown():
    d = decide(context=_ctx(), book_state=_book(seconds_since_last_exit=100))
    assert d["enter"] is False and "cooldown" in d["reason"]


def test_blocked_by_heat_cap():
    heavy = [{"max_loss_total": 58000, "underlying": "NIFTY", "bias": "BEARISH"}]
    d = decide(context=_ctx(), book_state=_book(open_positions=heavy, new_spread_max_loss=5000))
    assert d["enter"] is False and d["blocked_stage"] == "portfolio"
    assert "HEAT_CAP" in d["reason"]


def test_blocked_by_correlation():
    # Two bullish NIFTY spreads already open; a new PE (bullish) is the 3rd — today's failure.
    book = [{"max_loss_total": 2000, "underlying": "NIFTY", "bias": "BULLISH"},
            {"max_loss_total": 2000, "underlying": "NIFTY", "bias": "BULLISH"}]
    d = decide(context=_ctx(side="PE"), book_state=_book(open_positions=book))
    assert d["enter"] is False and "CORRELATION_CAP" in d["reason"]


def test_blocked_by_daily_loss_stop():
    d = decide(context=_ctx(), book_state=_book(realized_today=-20000))
    assert d["enter"] is False and "DAILY_LOSS_STOP" in d["reason"]


def test_config_override():
    d = decide(context=_ctx(), book_state=_book(), config={"credit_tp_frac": 0.5})
    assert d["enter"] is True and d["credit_tp_frac"] == 0.5
