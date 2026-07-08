"""Tests for RES-4/5/6 machinery: reentry, side_selector, portfolio_risk."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.reentry import evaluate_reentry
from core.side_selector import side_to_direction, select_sell_spread
from core.portfolio_risk import (
    position_risk, book_heat, evaluate_heat, evaluate_correlation,
    evaluate_daily_loss, evaluate_portfolio_gate,
)


# ── RES-4 re-entry ──────────────────────────────────────────────────────────────

def test_reentry_blocks_pyramiding():
    r = evaluate_reentry(has_open_position=True, trades_today=0, daily_cap=6)
    assert r["allow"] is False and "pyramiding" in r["reason"]


def test_reentry_blocks_at_daily_cap():
    r = evaluate_reentry(has_open_position=False, trades_today=6, daily_cap=6)
    assert r["allow"] is False and "daily trade cap" in r["reason"]


def test_reentry_blocks_in_cooldown():
    r = evaluate_reentry(has_open_position=False, trades_today=1, daily_cap=6,
                         seconds_since_last_exit=120, cooldown_s=300)
    assert r["allow"] is False and "cooldown" in r["reason"]


def test_reentry_allows_after_close_past_cooldown():
    r = evaluate_reentry(has_open_position=False, trades_today=2, daily_cap=6,
                         seconds_since_last_exit=600, cooldown_s=300)
    assert r["allow"] is True


# ── RES-5 side rotation ─────────────────────────────────────────────────────────

def test_side_to_direction():
    assert side_to_direction("PE") == "bullish"
    assert side_to_direction("CE") == "bearish"
    assert side_to_direction(None) is None


def test_select_sell_spread_bull_put():
    ctx = {"seller_decision": {"allow_sell": True, "side": "PE", "reasons": ["ok"]}}
    r = select_sell_spread(ctx)
    assert r["ok"] and r["structure"] == "credit_spread"
    assert r["side"] == "PE" and r["direction"] == "bullish"


def test_select_sell_spread_bear_call():
    ctx = {"seller_decision": {"allow_sell": True, "side": "CE", "reasons": ["ok"]}}
    r = select_sell_spread(ctx)
    assert r["ok"] and r["direction"] == "bearish"


def test_select_sell_spread_blocked_when_gates_fail():
    ctx = {"seller_decision": {"allow_sell": False, "side": None,
                               "reasons": ["regime TREND_DOWN not sell-safe"]}}
    r = select_sell_spread(ctx)
    assert r["ok"] is False and "TREND_DOWN" in r["reason"]


# ── RES-6 portfolio risk ────────────────────────────────────────────────────────

def _spread_pos(max_loss=20000, underlying="NIFTY", bias="BULLISH"):
    return {"max_loss_total": max_loss, "underlying": underlying, "bias": bias}


def test_position_risk_prefers_max_loss_total():
    assert position_risk({"max_loss_total": 1894.75}) == 1894.75
    assert position_risk({"planned_risk": 3000}) == 3000.0
    assert position_risk({"average_price": 100, "open_quantity": 65}) == 6500.0


def test_book_heat_sums_open_risk():
    assert book_heat([_spread_pos(20000), _spread_pos(15000)]) == 35000.0


def test_heat_cap_blocks_over_budget():
    book = [_spread_pos(30000), _spread_pos(25000)]  # 55k open
    ok = evaluate_heat(book, new_risk=4000, heat_budget=60000)
    bad = evaluate_heat(book, new_risk=10000, heat_budget=60000)
    assert ok["allow"] is True
    assert bad["allow"] is False and "HEAT_CAP" in bad["reason"]


def test_correlation_cap_blocks_same_bias():
    book = [_spread_pos(bias="BULLISH"), _spread_pos(bias="BULLISH")]
    r = evaluate_correlation(book, underlying="NIFTY", new_bias="BULLISH", max_same=2)
    assert r["allow"] is False and r["same_bias_open"] == 2
    # a bearish position on the same underlying is uncorrelated → allowed
    ok = evaluate_correlation(book, underlying="NIFTY", new_bias="BEARISH", max_same=2)
    assert ok["allow"] is True


def test_daily_loss_stop():
    assert evaluate_daily_loss(-5000, limit=15000)["allow"] is True
    assert evaluate_daily_loss(-16000, limit=15000)["allow"] is False


def test_portfolio_gate_combines_and_reports_first_blocker():
    # Today's failure: 2 bull-puts already open, a 3rd would breach correlation.
    book = [_spread_pos(bias="BULLISH"), _spread_pos(bias="BULLISH")]
    r = evaluate_portfolio_gate(book, new_risk=5000, underlying="NIFTY",
                                new_bias="BULLISH", realized_today=-2000,
                                heat_budget=60000, max_same=2, daily_loss_limit=15000)
    assert r["allow"] is False and "CORRELATION_CAP" in r["reason"]
    # loosen correlation → now heat/loss pass too → allowed
    r2 = evaluate_portfolio_gate(book, new_risk=5000, underlying="NIFTY",
                                 new_bias="BEARISH", realized_today=-2000,
                                 heat_budget=60000, max_same=2, daily_loss_limit=15000)
    assert r2["allow"] is True
