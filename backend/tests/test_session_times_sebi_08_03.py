"""Regression guards for the SEBI/NSE 2026-08-03 session change + the
hold-to-expiry / kill-switch fixes that shipped with it.

The change: NSE equity-derivatives close 15:30 -> 15:40 IST; cash gains a Closing
Auction Session 15:15-15:35 so CONTINUOUS cash trading ends 15:15.

Two directions, and confusing them is the whole risk:
  * F&O got 10 minutes LONGER  -> anything still stopping at 15:30 truncates.
  * Cash got 15 minutes SHORTER for continuous orders -> anything squaring off at
    or after 15:15 is trading into an auction.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import session_times as st  # noqa: E402


# ── the times themselves ──────────────────────────────────────────────────────

def test_nse_fo_closes_at_1540():
    assert st.NSE_FO_CLOSE_MINUTE == 15 * 60 + 40


def test_open_is_unchanged():
    assert st.OPEN_MINUTE == 9 * 60 + 15


def test_cash_continuous_ends_before_the_auction():
    """Continuous cash must stop when CAS starts, never after."""
    assert st.EQ_CONTINUOUS_CLOSE_MINUTE <= st.CAS_START_MINUTE
    assert st.CAS_START_MINUTE == 15 * 60 + 15
    assert st.CAS_END_MINUTE == 15 * 60 + 35


def test_bse_fo_stays_conservative_until_verified():
    """BSE was NOT verified to follow NSE. Being early costs opportunity; being
    late places orders into a closed session."""
    assert st.BSE_FO_CLOSE_MINUTE <= st.NSE_FO_CLOSE_MINUTE


def test_session_minutes_is_385_not_375():
    """This is the divisor in the §21.2 reachability law. A stale 375 overstates
    reachable decay and lets through spreads the law exists to veto."""
    assert st.session_minutes("NSE_FO") == 385
    assert st.expected_bar_count("NSE_FO") == 385


# ── square-off ordering: the invariant that keeps orders out of the auction ────

def test_equity_squareoff_completes_before_the_cash_auction():
    assert st.EQUITY_SQUAREOFF_MINUTE < st.CAS_START_MINUTE


def test_spread_squareoff_is_before_the_derivatives_close():
    assert st.SPREAD_SQUAREOFF_MINUTE < st.NSE_FO_CLOSE_MINUTE


def test_post_close_tasks_run_after_every_close():
    """The 15:35 capture flush / auto-pause used to be post-market; 15:35 is now
    mid-session for NSE F&O. Anything post-close must clear the LAST close."""
    assert st.POST_CLOSE_MINUTE > st.NSE_FO_CLOSE_MINUTE
    assert st.POST_CLOSE_MINUTE > st.CAS_END_MINUTE
    assert st.POST_CLOSE_MINUTE >= st.LAST_CLOSE_MINUTE


# ── the guard that replaced three copy-pasted expressions ─────────────────────

@pytest.mark.parametrize("hour,minute,expected", [
    (9, 14, False),   # pre-open
    (9, 15, True),    # open
    (12, 0, True),
    (15, 29, True),
    (15, 30, True),   # the OLD close - was the last True, must no longer be
    (15, 35, True),   # inside the extension; the old guards said False here
    (15, 40, True),   # close, inclusive
    (15, 41, False),
])
def test_in_session_covers_the_extension(hour, minute, expected):
    assert st.in_session(hour, minute, "NSE_FO") is expected


def test_in_session_is_tighter_for_cash():
    """Cash stops at 15:15; the same wall-clock minute differs by segment."""
    assert st.in_session(15, 30, "NSE_FO") is True
    assert st.in_session(15, 30, "NSE_EQ") is False
    assert st.in_session(15, 14, "NSE_EQ") is True


def test_cash_auction_window():
    assert st.is_cash_auction_window(15 * 60 + 14) is False
    assert st.is_cash_auction_window(15 * 60 + 20) is True
    assert st.is_cash_auction_window(15 * 60 + 35) is False


# ── the consumers actually picked the new values up ───────────────────────────

def test_market_clock_uses_the_new_close():
    from core import market_clock
    from core.market_domains import DomainType
    assert market_clock.NSE_CLOSE_MINUTE == st.NSE_FO_CLOSE_MINUTE
    _, fo_close, _ = market_clock.SEGMENT_WINDOWS[DomainType.NSE_FO]
    _, eq_close, _ = market_clock.SEGMENT_WINDOWS[DomainType.NSE_EQ]
    assert fo_close == 15 * 60 + 40
    assert eq_close == 15 * 60 + 15


def test_both_session_modules_agree():
    """They each carried their own copy of the window table and could drift."""
    from core import market_clock
    from core.market_session_service import SEGMENT_WINDOWS as SVC
    from core.market_domains import DomainType
    for dom in (DomainType.NSE_FO, DomainType.BSE_FO, DomainType.NSE_EQ, DomainType.BSE_EQ):
        assert market_clock.SEGMENT_WINDOWS[dom][:2] == SVC[dom][:2], dom


def test_reachability_law_uses_385():
    from core import spread_builder
    assert spread_builder.MARKET_MINUTES_PER_DAY == 385.0


def test_option_minute_store_last_bar_is_1539():
    from core import options_minute_store as oms
    assert oms.SESSION_END == (15, 39)
    assert len(oms.expected_minutes("2026-08-03")) == 385


# ── kill-switch scoping (task A) ──────────────────────────────────────────────

def _strategy(structure="credit_spread", exit_mode="", risk_exit_mode=""):
    return {"visual_config": {"options": {"structure": structure, "exit_mode": exit_mode},
                              "risk": {"exit_mode": risk_exit_mode}}}


def test_hold_to_expiry_detection():
    from core import loss_killswitch as ks
    assert ks._is_hold_to_expiry(_strategy(exit_mode="expiry")) is True
    assert ks._is_hold_to_expiry(_strategy(risk_exit_mode="hold_to_expiry")) is True
    assert ks._is_hold_to_expiry(_strategy()) is False


def test_defined_risk_detection():
    from core import loss_killswitch as ks
    assert ks._is_defined_risk(_strategy("credit_spread")) is True
    assert ks._is_defined_risk(_strategy("debit_spread")) is True
    # A naked single leg is NOT defined-risk and must never get the exemption.
    assert ks._is_defined_risk(_strategy("single_leg")) is False


def test_killswitch_exemption_requires_both_conditions():
    """The exemption is narrow on purpose: a naked position held to expiry has an
    unbounded loss and must still be swept."""
    from core import loss_killswitch as ks
    naked_hte = _strategy("single_leg", exit_mode="expiry")
    assert ks._is_hold_to_expiry(naked_hte) is True
    assert ks._is_defined_risk(naked_hte) is False


# ── DTE policy must not silently veto a hold-to-expiry sleeve ─────────────────

def test_dte_policy_stands_down_a_far_expiry_intraday_seller():
    """Unchanged behaviour for the intraday book the 259-trade study measured."""
    from core.dte_policy import evaluate
    from datetime import date
    p = evaluate(expiry="2026-08-20", regime="RANGE", today=date(2026, 8, 3))
    assert p.allow is False
    assert "past the seller's edge window" in p.reason


def test_dte_policy_exempts_hold_to_expiry():
    """The DTE study measured ONLY early exits, so it cannot bind a position that
    actually rides to settlement. Without this the 5-15 DTE HTE sleeve would be
    vetoed on every entry and could never run (the §22.3 defect class)."""
    from core.dte_policy import evaluate
    from datetime import date
    p = evaluate(expiry="2026-08-20", regime="RANGE", today=date(2026, 8, 3),
                 hold_to_expiry=True)
    assert p.allow is True
    assert p.telemetry.get("hold_to_expiry") is True


def test_hold_to_expiry_exemption_still_rejects_an_expired_contract():
    from core.dte_policy import evaluate
    from datetime import date
    p = evaluate(expiry="2026-08-01", regime="RANGE", today=date(2026, 8, 3),
                 hold_to_expiry=True)
    assert p.allow is False
