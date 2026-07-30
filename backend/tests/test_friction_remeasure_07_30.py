"""2026-07-30 friction re-measurement.

The 3%/leg slippage assumption was a guess, and it was wrong by ~12x. Measured
over 4,587 real bid/ask quotes stored on QuantG's own signals:

    bid-ask spread      = 0.252% of mid (median)  -> 3.0% was 11.9x the FULL spread
    half-spread crossed = NIFTY 0.122%  SENSEX 0.126%  BANKNIFTY 0.157%
    brokerage + taxes   = Rs14.78 round trip per lot (the constant said Rs300)

Three things were being driven by that one wrong number: the cost floor demanded
Rs900 of bankable profit where the true 3x bar is ~Rs78 (6,009 candidates vetoed in
a week), paper fills paid ~Rs280/lot/trade of imaginary cost, and BOTH OOS judges
computed every NO_EDGE_NEGATIVE verdict at ~12x real friction.

Founder-approved: 0.5%/leg (~4x the measured half-spread, a deliberate buffer for
0-DTE far-OTM widening and impact at size) and tp_frac 0.25 (reached by 33% of
trades vs 12% for 0.50).
"""

import importlib

import pytest


def _reload_builder(monkeypatch, slip: str, flat: str):
    monkeypatch.setenv("PAPER_SPREAD_SLIPPAGE_PCT", slip)
    monkeypatch.setenv("SPREAD_ROUND_TRIP_COST_PER_LOT", flat)
    import core.spread_builder as sb
    return importlib.reload(sb)


def test_measured_friction_is_an_order_of_magnitude_below_the_old_model(monkeypatch):
    """The headline number. NIFTY legs 70.35, lot 65."""
    sb = _reload_builder(monkeypatch, "0.005", "25")
    try:
        new = sb.round_trip_friction(70.35, 65)
        assert 25 <= new <= 80, f"expected ~Rs46, got {new}"
        old = _reload_builder(monkeypatch, "0.03", "300").round_trip_friction(70.35, 65)
        assert old / new > 5, f"old {old} vs new {new} — should be several-fold cheaper"
    finally:
        monkeypatch.undo()
        importlib.reload(sb)


def test_the_veto_that_blocked_qgo1_today_now_passes(monkeypatch):
    """Live 2026-07-30: QG-O1 was vetoed on 'credit 11.90 on width 200'. At the
    approved tp_frac and corrected friction that same contract is fundable."""
    sb = _reload_builder(monkeypatch, "0.005", "25")
    try:
        out = sb.credit_cost_floor(11.90 * 2, 200.0, lot_size=65, tp_frac=0.25,
                                   leg_premium_sum=70.35)
        # ratio must still be judged on its own merits...
        assert out["floor_passed"], "corrected friction must make this fundable"
    finally:
        monkeypatch.undo()
        importlib.reload(sb)


def test_flat_floor_still_protects_thin_premium_contracts(monkeypatch):
    """The proportional model must never fall below the real fixed cost, or a
    tiny-premium contract would look free."""
    sb = _reload_builder(monkeypatch, "0.005", "25")
    try:
        assert sb.round_trip_friction(1.0, 20) == 25.0
        assert sb.round_trip_friction(None, None) == 25.0
    finally:
        monkeypatch.undo()
        importlib.reload(sb)


def test_banknifty_still_charged_more_than_nifty(monkeypatch):
    """Friction stays premium-proportional — BANKNIFTY's fat premium must still
    cost more per lot than NIFTY's, which is what the flat constant got wrong."""
    sb = _reload_builder(monkeypatch, "0.005", "25")
    try:
        assert sb.round_trip_friction(900.0, 30) > sb.round_trip_friction(70.35, 65)
    finally:
        monkeypatch.undo()
        importlib.reload(sb)


def test_reachable_tp_clears_the_cost_floor_with_room(monkeypatch):
    """tp 0.25 banks LESS than 0.45, so it only works because friction fell. Both
    halves of the change are load-bearing — verify they compose."""
    sb = _reload_builder(monkeypatch, "0.005", "25")
    try:
        out = sb.credit_cost_floor(50.0, 300.0, lot_size=20, tp_frac=0.25,
                                   leg_premium_sum=129.0)
        assert out["passed"], out
        assert out["cost_multiple"] >= 3.0
    finally:
        monkeypatch.undo()
        importlib.reload(sb)


def test_old_friction_would_have_failed_the_same_trade(monkeypatch):
    """Guard against quietly reverting: at 3%/leg the reachable-tp geometry is
    refused, which is exactly the trap the book was in."""
    sb = _reload_builder(monkeypatch, "0.03", "300")
    try:
        out = sb.credit_cost_floor(50.0, 300.0, lot_size=20, tp_frac=0.25,
                                   leg_premium_sum=129.0)
        assert not out["floor_passed"]
    finally:
        monkeypatch.undo()
        importlib.reload(sb)


def test_intraday_judge_slippage_default_corrected():
    from core.intraday_options_backtest import IntradayCosts

    assert IntradayCosts().slippage_pct <= 0.01, "judge must not cost trades at 2%/side"
