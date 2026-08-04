"""A: "never went green" early cut.  B: book-wide credit/width ceiling.

Both derived from the 94-closed-spread study (2026-07-30):

  A — 54% of LOSERS were never meaningfully green (peak <= Rs50); the
      spread-time-exit pool (38% of trades) median-peaked at 0.3% of credit.
      Winners median-peak at 29%. So a spread with no life after a fair window is
      overwhelmingly a dead loser being held in hope. Cut it.

  B — ONE ratio explains almost every strategy's lifetime record: credit/width
      0.09-0.21 wins at every DTE; <0.09 loses (too thin), >0.22 loses (short
      strike too near the money). QG-O11 0.255 -Rs8,075 and RAE-BANKNIFTY 0.246
      -Rs8,667 are the two biggest loss pools, both above 0.22.

Context these encode the opposite of: averaging DOWN into losers, simulated on the
real book, moves it from -Rs21,934 to -Rs60,247 (2x) / -Rs98,561 (3x).
"""

import importlib

import pytest

from core import dynamic_exit as de


# ── A: no-progress cut ─────────────────────────────────────────────────────


def test_dead_trade_is_cut_after_the_window():
    """Held 25 min, peak never cleared 8% of a 1300-rupee credit (Rs104) -> cut.

    dte_days is now REQUIRED for the rule to fire (2026-08-04): the reachability
    gate fails closed, because an unresolvable DTE used to skip it entirely and
    silently restore the flat 20-minute window. 0 DTE here keeps this test's
    original intent — near expiry, 25 minutes IS enough decay to judge on.
    """
    # held 60 min at 0 DTE = 15.6% of the session, comfortably past the 8% bar,
    # so the reachability gate permits the judgement (25 min delivers only 6.5%).
    r = de.no_progress_exit(peak_pnl=20.0, held_minutes=60.0,
                            net_credit=20.0, qty=65, lot_size=65, lots=1,
                            leg_premium_sum=70.0, dte_days=0)
    assert r == "spread-no-progress"


def test_working_trade_is_left_alone():
    """Same trade, but peak reached 30% of credit -> it is alive, do not cut."""
    r = de.no_progress_exit(peak_pnl=400.0, held_minutes=25.0,
                            net_credit=20.0, qty=65, lot_size=65, lots=1,
                            leg_premium_sum=70.0)
    assert r is None


def test_not_cut_before_the_window():
    """A trade flat at 5 minutes has not had its chance yet."""
    r = de.no_progress_exit(peak_pnl=0.0, held_minutes=5.0,
                            net_credit=20.0, qty=65, lot_size=65, lots=1)
    assert r is None


def test_threshold_is_floored_at_real_friction():
    """A thin-credit spread must not be cut for failing a rupee bar it never could
    clear — the fraction floors at real round-trip friction, not below."""
    # credit_money = 2*65 = 130; 8% = Rs10.4, but friction floor is higher.
    # dte_days=0 (near expiry) so the reachability gate lets the rule be applied —
    # see test_dead_trade_is_cut_after_the_window.
    # A thin credit makes the FRICTION floor (~Rs46) the binding bar, and decay has
    # to have delivered at least that much before the trade can be judged against
    # it: 150 min at 0 DTE = 39% of the session = Rs50 of the Rs130 credit.
    r = de.no_progress_exit(peak_pnl=30.0, held_minutes=150.0,
                            net_credit=2.0, qty=65, lot_size=65, lots=1,
                            leg_premium_sum=70.0, dte_days=0)
    # NIFTY friction ~Rs46 > Rs30 peak -> still judged lifeless
    assert r == "spread-no-progress"


def test_missing_inputs_never_crash_the_monitor():
    assert de.no_progress_exit(peak_pnl=None, held_minutes=None,
                               net_credit=0.0, qty=0) is None
    assert de.no_progress_exit(peak_pnl=100.0, held_minutes=30.0,
                               net_credit=0.0, qty=0) is None


def test_disabled_is_a_no_op(monkeypatch):
    monkeypatch.setenv("DYN_EXIT_NO_PROGRESS_ENABLED", "false")
    importlib.reload(de)
    try:
        assert de.no_progress_exit(peak_pnl=0.0, held_minutes=60.0,
                                   net_credit=20.0, qty=65) is None
    finally:
        monkeypatch.delenv("DYN_EXIT_NO_PROGRESS_ENABLED", raising=False)
        importlib.reload(de)


# ── B: book-wide ratio ceiling ─────────────────────────────────────────────


def _reload_sb(monkeypatch, ceiling: str):
    monkeypatch.setenv("CREDIT_SPREAD_MAX_CREDIT_RATIO", ceiling)
    import core.spread_builder as sb
    return importlib.reload(sb)


def test_ceiling_blocks_the_QGO11_and_BANKNIFTY_shape(monkeypatch):
    """Ratio 0.25 (QG-O11) and 0.246 (RAE-BANKNIFTY) — the two biggest loss pools
    — must be refused book-wide, no near-expiry context required."""
    sb = _reload_sb(monkeypatch, "0.22")
    try:
        for ratio_credit, width in ((50.0, 200.0), (49.2, 200.0)):  # 0.25, 0.246
            out = sb.credit_cost_floor(ratio_credit, width, lot_size=20, tp_frac=0.25,
                                       leg_premium_sum=129.0)
            assert not out["ratio_max_passed"]
            assert not out["passed"]
    finally:
        monkeypatch.undo()
        importlib.reload(sb)


def test_ceiling_passes_the_winning_band(monkeypatch):
    """The winners live at 0.09-0.21 — all must still build."""
    sb = _reload_sb(monkeypatch, "0.22")
    try:
        for ratio_credit, width in ((17.8, 200.0), (32.0, 200.0), (42.0, 200.0)):  # ~0.089, 0.16, 0.21
            out = sb.credit_cost_floor(ratio_credit, width, lot_size=20, tp_frac=0.25,
                                       leg_premium_sum=129.0)
            assert out["ratio_max_passed"], f"credit {ratio_credit} should pass the ceiling"
    finally:
        monkeypatch.undo()
        importlib.reload(sb)


def test_ceiling_off_by_empty_string(monkeypatch):
    sb = _reload_sb(monkeypatch, "")
    try:
        out = sb.credit_cost_floor(50.0, 200.0, lot_size=20, tp_frac=0.25,
                                   leg_premium_sum=129.0)
        assert out["ratio_max_passed"], "empty ceiling = no upper bound"
    finally:
        monkeypatch.undo()
        importlib.reload(sb)
