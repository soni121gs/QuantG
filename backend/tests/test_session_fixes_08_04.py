"""Regression guards for the 2026-08-04 session defects.

Each test pins a defect that was live that day. Read the docstrings before
changing any of these — several pin behaviour that LOOKS wrong until you know
which failure it prevents.
"""
from __future__ import annotations

import importlib
import os
from datetime import date

import pytest

# These tests reload core.spread_builder / core.dynamic_exit to exercise their
# env-read constants. Module constants are read at IMPORT time, so a reload leaves
# the patched values baked in for every test that runs afterwards — that is exactly
# the mechanism behind the existing suite debt described in CLAUDE.md §25.5
# (test_friction_remeasure_07_30 reloads spread_builder and later tests fail
# against values it left behind). Restore both modules, with the environment they
# were originally imported under, so this file cannot do the same thing.
_RELOAD_ENV = (
    "MAX_RISK_PER_TRADE_RUPEES",
    "DYN_EXIT_NO_PROGRESS_REQUIRE_REACHABLE",
)


@pytest.fixture(autouse=True)
def _restore_reloaded_modules():
    saved = {k: os.environ.get(k) for k in _RELOAD_ENV}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import core.dynamic_exit
    import core.spread_builder
    importlib.reload(core.spread_builder)
    importlib.reload(core.dynamic_exit)


# ── 1. no-progress reachability must FAIL CLOSED on an unknown DTE ──────────────

def _dyn(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import core.dynamic_exit as de
    return importlib.reload(de)


def test_no_progress_suppressed_when_dte_unknown(monkeypatch):
    """THE 2026-08-04 BUG. The gate read `... and dte_days is not None`, so an
    unresolvable DTE skipped the gate entirely and restored the flat 20-minute
    window the fix existed to remove. All 8 spreads that day were cut at exactly
    20 minutes, -Rs2,337, with the guard deployed and enabled.

    An input we cannot resolve must disable the RULE, not the SAFEGUARD.
    """
    de = _dyn(monkeypatch, DYN_EXIT_NO_PROGRESS_REQUIRE_REACHABLE="true")
    # A trade that would otherwise be cut: held past the window, peak far below the bar.
    assert de.no_progress_exit(
        peak_pnl=5.0, held_minutes=25.0, net_credit=100.0, qty=20, dte_days=None,
    ) is None


def test_no_progress_still_cuts_when_decay_was_available(monkeypatch):
    """The rule must still fire when the bar WAS reachable — near expiry, where
    decay is fast, a dead trade is still a dead trade."""
    de = _dyn(monkeypatch, DYN_EXIT_NO_PROGRESS_REQUIRE_REACHABLE="true")
    # 0 DTE, held nearly the whole session: decay had every chance to deliver.
    assert de.no_progress_exit(
        peak_pnl=1.0, held_minutes=300.0, net_credit=100.0, qty=20,
        dte_days=0, session_minutes=375.0,
    ) == "spread-no-progress"


def test_no_progress_holds_a_far_dte_trade_inside_the_window(monkeypatch):
    """2.4 DTE at 20 minutes supplies ~1.4% of credit against an 8% bar — the
    exact shape of the eight SENSEX spreads cut on 2026-08-04."""
    de = _dyn(monkeypatch, DYN_EXIT_NO_PROGRESS_REQUIRE_REACHABLE="true")
    assert de.no_progress_exit(
        peak_pnl=95.0, held_minutes=20.0, net_credit=124.85, qty=20,
        dte_days=2.4, session_minutes=375.0,
    ) is None


# ── 2. expiry settles at intrinsic ─────────────────────────────────────────────

def test_option_intrinsic_both_sides():
    from core.spread_builder import option_intrinsic
    assert option_intrinsic(24600, "CE", 24614.9) == pytest.approx(14.9)
    assert option_intrinsic(24600, "CE", 24463.45) == 0.0
    assert option_intrinsic(24550, "PE", 24463.45) == pytest.approx(86.55)
    assert option_intrinsic(24550, "PE", 24614.9) == 0.0


def test_settle_legs_at_intrinsic_fails_closed_on_bad_input():
    """A settlement path that invents a price is worse than one that admits it has
    none — the caller must be able to tell the difference and fall back."""
    from core.spread_builder import settle_legs_at_intrinsic
    short = {"strike": 24550, "option_type": "PE"}
    long = {"strike": 24350, "option_type": "PE"}
    assert settle_legs_at_intrinsic(short, long, 0) is None
    assert settle_legs_at_intrinsic(short, long, None) is None
    assert settle_legs_at_intrinsic(short, {"strike": None, "option_type": "PE"}, 24500) is None
    assert settle_legs_at_intrinsic(None, long, 24500) is None


def test_settle_legs_reproduces_the_08_04_settlement():
    """The seven positions settled on 2026-08-04 priced consistently against an
    index level of 24614.9. Pin the arithmetic that reproduces it, and the very
    different answer 150 points lower — the gap was Rs12,659 on that day's book.
    """
    from core.spread_builder import settle_legs_at_intrinsic
    short = {"strike": 24600.0, "option_type": "CE"}
    long = {"strike": 25100.0, "option_type": "CE"}
    at_tick = settle_legs_at_intrinsic(short, long, 24614.9)
    assert at_tick == {"short": 14.9, "long": 0.0}
    at_frozen = settle_legs_at_intrinsic(short, long, 24463.45)
    assert at_frozen == {"short": 0.0, "long": 0.0}


# ── 3. book-wide per-trade rupee cap ───────────────────────────────────────────

def _sb(monkeypatch, cap: str):
    monkeypatch.setenv("MAX_RISK_PER_TRADE_RUPEES", cap)
    import core.spread_builder as sb
    return importlib.reload(sb)


def test_risk_cap_trims_the_mean_reversion_outlier(monkeypatch):
    """IDX NIFTY Mean-Reversion Fade: max_loss 48.65/unit, lot 65 => Rs3,162/lot.
    At its configured 20,000 budget that is 6 lots / ~Rs19k of risk (it took 5 and
    lost Rs8,077). The cap holds it to 2 lots / ~Rs6.3k."""
    sb = _sb(monkeypatch, "8000")
    assert sb.lots_for_risk(48.65, 65, 20000) == 6            # unchanged, still pure
    assert sb.cap_lots_by_risk(6, 48.65, 65) == 2


def test_risk_cap_never_stands_a_strategy_down(monkeypatch):
    """THE REASON THIS IS NOT INSIDE lots_for_risk. Defined risk per LOT varies
    hugely by design (Rs491 tail hedge, Rs12,873 SENSEX seller, Rs30,673 HTE). A
    ceiling that could return 0 would permanently stand those sleeves down — a
    trading decision disguised as a sizing one. It floors at 1 lot always.
    """
    sb = _sb(monkeypatch, "8000")
    assert sb.cap_lots_by_risk(1, 643.66, 20) == 1     # SENSEX seller, 12.9k/lot
    assert sb.cap_lots_by_risk(1, 165.16, 65) == 1     # QG-O1, 10.7k/lot
    assert sb.cap_lots_by_risk(1, 471.89, 65) == 1     # HTE sleeve, 30.7k/lot
    # and lots_for_risk itself keeps its pure contract (existing invariants rely on it)
    assert sb.lots_for_risk(630.79, 20, 13000.0) >= 1


def test_risk_cap_leaves_small_positions_untouched(monkeypatch):
    sb = _sb(monkeypatch, "8000")
    assert sb.cap_lots_by_risk(6, 9.25, 65) == 6       # tail hedge: 6 lots = Rs3.6k


def test_risk_cap_disabled_restores_old_behaviour(monkeypatch):
    sb = _sb(monkeypatch, "0")
    assert sb.cap_lots_by_risk(6, 48.65, 65) == 6


# ── 4. router cross-checks every seller-permissive fine label ──────────────────

def test_coarse_trend_stands_a_seller_down_on_fine_inside_quiet():
    """THE 2026-08-04 REGIME BUG. The cross-check fired only when the fine read
    resolved to RANGE. INSIDE_QUIET is equally a seller-home label, so on a day
    when fine read INSIDE_QUIET and coarse read TREND_DOWN all midday, six SENSEX
    put-spread entries went through for -Rs3,454 — selling puts into the slide.
    """
    from core.regime_router import route
    d = route("INSIDE_QUIET", 0.62, specialist="range_seller",
              fallback_regime="TREND_DOWN",
              owned_regimes=["RANGE", "INSIDE_QUIET"], structure="credit_spread")
    assert d.stand_down, d.reasons


def test_range_cross_check_still_works():
    """The original R1 behaviour must be unchanged."""
    from core.regime_router import route
    d = route("RANGE", 0.40, specialist="range_seller", fallback_regime="TREND_DOWN",
              owned_regimes=["RANGE", "INSIDE_QUIET"], structure="credit_spread")
    assert d.stand_down, d.reasons


def test_cross_check_does_not_veto_a_strategy_that_owns_the_coarse_regime():
    """Conservative-only: the cross-check can never authorise a trade the fine
    read refused, and must not veto a strategy that legitimately owns the coarse
    regime. The tail hedge declares all six, so a coarse TREND_DOWN is its
    business — this is what §26.3's declared-ownership fix protects."""
    from core.regime_router import route
    d = route("INSIDE_QUIET", 0.24, specialist="tail_hedge", fallback_regime="TREND_DOWN",
              owned_regimes=["RANGE", "INSIDE_QUIET", "HIGH_VOL_CHOP", "EVENT",
                             "TREND_UP", "TREND_DOWN"],
              structure="debit_spread")
    assert not d.stand_down, d.reasons


def test_affirmative_non_seller_labels_are_still_trusted():
    """A fine TREND/CHOP read is a real detection and must not be re-litigated
    against the coarse organ — that was the original design and it stands."""
    from core.regime_router import route
    d = route("TREND_UP", 0.95, specialist="trend_delta1", fallback_regime="RANGE",
              owned_regimes=["TREND_UP", "TREND_DOWN"], structure="single_leg")
    assert not d.stand_down, d.reasons


# ── 5. tail hedge tenor ────────────────────────────────────────────────────────

def test_select_expiry_refuses_0dte_for_a_windowed_hedge():
    """With a 5-15 DTE window, the Tuesday weekly (0 DTE) must NOT be chosen; an
    8-day expiry is. Standing down beats substituting a tenor the strategy never
    asked for (§25.4b)."""
    from core.dte_policy import select_expiry
    today = date(2026, 8, 4)
    out = select_expiry(["2026-08-04", "2026-08-11", "2026-08-25"],
                        expiry_offset=0, min_dte=5, max_dte=15, today=today)
    assert out["expiry"] == "2026-08-11"
    assert out["dte"] == 7


def test_select_expiry_stands_down_when_nothing_fits():
    from core.dte_policy import select_expiry
    out = select_expiry(["2026-08-04", "2026-08-05"], expiry_offset=0,
                        min_dte=5, max_dte=15, today=date(2026, 8, 4))
    assert out["expiry"] is None


def test_tail_hedge_seed_carries_the_window_and_single_entry():
    """The seed template and the live row must agree; the migration writes the
    same values to the DB because startup template sync is disabled (§20.1)."""
    import importlib.util
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "seed_tail_hedge", os.path.join(here, "scripts", "seed_tail_hedge.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.HEDGE_OPTIONS["min_dte_days"] == 5
    assert mod.HEDGE_OPTIONS["max_dte_days"] == 15
    assert mod.HEDGE_RISK["max_trades_day"] == 1
