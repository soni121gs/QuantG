"""Regression guards for the 2026-08-06 audit fixes (F1, F2, F3/F4, F5).

Each test pins a defect that was live in production on 2026-08-06 and would otherwise
be invisible: three of the five were silent (no error, no log, correct-looking config).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1] / "server.py"
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------- F5: token freshness

def _fresh(obtained_iso, now_ist):
    from core.upstox_auth_request import token_is_fresh
    return token_is_fresh(obtained_iso, now_ist=now_ist)


def test_utc_labelled_ist_clock_does_not_false_alarm():
    """The exact 2026-08-06 production signature.

    server.py's `_ist_now()` is `datetime.now(utc) + 5:30` — IST wall clock, UTC tzinfo.
    The boundary used to be built from that, landing at 03:30 UTC = 09:00 IST, so a token
    stored at 08:46 IST was judged stale and the 09:05 CRITICAL alarm fired every day the
    08:45 flow worked correctly.
    """
    obtained = "2026-08-06T03:16:36.374773+00:00"          # 08:46:36 IST
    fake_ist = datetime(2026, 8, 6, 9, 5, 34, tzinfo=timezone.utc)   # _ist_now() shape
    assert _fresh(obtained, fake_ist) is True


def test_genuine_ist_aware_clock_agrees_with_the_utc_labelled_one():
    """Both caller shapes must produce the SAME verdict — that is the fix's whole point."""
    obtained = "2026-08-06T03:16:36.374773+00:00"
    fake_ist = datetime(2026, 8, 6, 9, 5, 34, tzinfo=timezone.utc)
    real_ist = datetime(2026, 8, 6, 9, 5, 34, tzinfo=IST)
    assert _fresh(obtained, fake_ist) == _fresh(obtained, real_ist) is True


def test_yesterdays_token_is_still_correctly_stale():
    """The alarm must keep firing when it should — the fix must not silence it."""
    obtained = "2026-08-05T14:30:00+00:00"                 # 20:00 IST yesterday
    assert _fresh(obtained, datetime(2026, 8, 6, 9, 5, tzinfo=timezone.utc)) is False


def test_token_from_just_after_the_boundary_is_fresh():
    obtained = "2026-08-05T22:01:00+00:00"                 # 03:31 IST today
    assert _fresh(obtained, datetime(2026, 8, 6, 9, 5, tzinfo=timezone.utc)) is True


def test_token_from_just_before_the_boundary_is_stale():
    obtained = "2026-08-05T21:59:00+00:00"                 # 03:29 IST today
    assert _fresh(obtained, datetime(2026, 8, 6, 9, 5, tzinfo=timezone.utc)) is False


@pytest.mark.parametrize("bad", [None, "", "not-a-date"])
def test_unparseable_obtained_at_fails_closed(bad):
    assert _fresh(bad, datetime(2026, 8, 6, 9, 5, tzinfo=timezone.utc)) is False


# ------------------------------------------------- F1: hold-to-expiry stale exemption

def _lifecycle_src() -> str:
    src = SERVER.read_text(encoding="utf-8", errors="replace")
    start = src.index("async def _daily_paper_lifecycle_for_user")
    return src[start:start + 6000]


def test_stale_sweep_exempts_hold_to_expiry_strategies():
    """A hold-to-expiry sleeve carries overnight by design.

    Quarantining it into STALE_NEEDS_REVIEW — a status position_monitor never scans —
    froze it for the whole session: never marked, never exit-checked, unable to reach
    expiry-settlement. Source-level because the loop is not unit-testable in isolation
    (same approach as the §25.4b / §26.4b positional invariants).
    """
    body = _lifecycle_src()
    assert "_hte_strategy_ids" in body, "stale sweep no longer computes the exempt set"
    assert 'visual_config.options.exit_mode' in body
    assert '"expiry"' in body
    assert '"$nin": list(_hte_strategy_ids)' in body, "exempt set is computed but not applied"


def test_stale_exemption_covers_both_the_ledger_and_the_ui_mirror():
    body = _lifecycle_src()
    assert body.count('"$nin": list(_hte_strategy_ids)') >= 2, (
        "strategy_positions and the db.positions mirror must agree, or Positions shows "
        "a quarantined row for a sleeve the ledger is correctly still holding")


def test_exempt_set_is_only_applied_when_non_empty():
    """An empty $nin would be harmless, but the guard documents intent and avoids a
    pointless index-wide filter on a fresh install with no strategies."""
    body = _lifecycle_src()
    assert "if _hte_strategy_ids:" in body


# ----------------------------------------------------- F2: single-leg short_delta read

def _delta_block() -> str:
    src = SERVER.read_text(encoding="utf-8", errors="replace")
    i = src.index("if OPTION_DELTA_SELECTION_ENABLED:")
    return src[i:i + 2600]


def test_single_leg_delta_selection_prefers_configured_short_delta():
    """`short_delta` was read only in the spread branch, so for single_leg it was
    decorative: the three RAE "Trend Delta-1" riders configured 0.80 (deep ITM, ~zero
    theta) measurably bought delta 0.39-0.42 and lost 5 of 7 trades to theta-decay
    exits — an exit reason a real delta-1 rider cannot produce."""
    body = _delta_block()
    assert '"short_delta"' in body, "single-leg path still ignores the configured delta"
    assert "target_delta_for_style" in body, "style table must remain the fallback"
    assert body.index('"short_delta"') < body.index("target_delta_for_style"), (
        "configured short_delta must be consulted BEFORE the risk-style default")


def test_configured_short_delta_is_clamped_and_survives_junk():
    body = _delta_block()
    assert "max(0.05, min(0.95," in body, "an out-of-range delta must be clamped"
    assert "except (TypeError, ValueError)" in body, (
        "a non-numeric short_delta must fall back to the style table, not raise on the "
        "order path")


# ------------------------------------------------------- F3/F4: the migration contract

def _migration():
    import importlib.util
    p = Path(__file__).resolve().parents[1] / "scripts" / "fix_seller_geometry_and_dte_08_06.py"
    spec = importlib.util.spec_from_file_location("_mig_08_06", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_restores_break_even_to_the_pre_retune_level():
    m = _migration()
    tp = 0.25
    be = m.NEW_SL_MULT / (m.NEW_SL_MULT + tp)
    assert 0.64 <= be <= 0.645, f"break-even {be:.3f} is not the ~64.3% target"


def test_migration_targets_only_intraday_credit_sellers():
    m = _migration()
    seller = {"visual_config": {"options": {
        "structure": "credit_spread", "credit_tp_frac": 0.25, "credit_sl_mult": 0.6}}}
    assert m._is_intraday_credit_seller(seller) is True


@pytest.mark.parametrize("opts", [
    # hold-to-expiry: no intraday geometry, and the DTE stand-down must not apply (§25.4)
    {"structure": "credit_spread", "exit_mode": "expiry",
     "credit_tp_frac": 0.25, "credit_sl_mult": 0.6},
    # debit spreads are buyers — exempt from the seller laws
    {"structure": "debit_spread", "credit_tp_frac": 0.25, "credit_sl_mult": 0.6},
    # single-leg
    {"structure": "single_leg", "credit_tp_frac": 0.25, "credit_sl_mult": 0.6},
    # a spread with no configured geometry is not a seller we re-cut
    {"structure": "credit_spread"},
])
def test_migration_skips_everything_else(opts):
    m = _migration()
    assert m._is_intraday_credit_seller({"visual_config": {"options": opts}}) is False


def test_hold_to_expiry_detected_via_the_risk_field_too():
    m = _migration()
    s = {"visual_config": {"options": {"structure": "credit_spread",
                                       "credit_tp_frac": 0.25, "credit_sl_mult": 0.6},
                           "risk": {"exit_mode": "hold_to_expiry"}}}
    assert m._is_intraday_credit_seller(s) is False


def test_dte_window_is_the_measured_bucket():
    m = _migration()
    assert (m.NEW_MIN_DTE, m.NEW_MAX_DTE) == (0, 2), (
        "window must keep the strongly-positive DTE-0 bucket and block DTE 3+")


def test_min_dte_zero_survives_the_config_reader():
    """`0` is falsy — a reader written `opts.get(k) or None` would silently drop it and
    hand back the legacy positional expiry, reintroducing the decorative-config trap."""
    from core.dte_policy import select_expiry
    today = datetime(2026, 8, 6).date()
    out = select_expiry(["2026-08-06", "2026-08-11", "2026-08-13"],
                        min_dte=0, max_dte=2, today=today)
    assert out["expiry"] == "2026-08-06" and out["dte"] == 0


def test_dte_window_stands_down_rather_than_substituting():
    """Fail-closed is the point: substituting a tenor the strategy did not ask for
    produces P&L attributed to a geometry that never ran."""
    from core.dte_policy import select_expiry
    out = select_expiry(["2026-08-11", "2026-08-18"], min_dte=0, max_dte=2,
                        today=datetime(2026, 8, 6).date())
    assert out["expiry"] is None and out["reason"]


def test_no_window_configured_keeps_legacy_positional_behaviour():
    """Every strategy without a window must be byte-identical to before."""
    from core.dte_policy import select_expiry
    out = select_expiry(["2026-08-11", "2026-08-18"], expiry_offset=0,
                        today=datetime(2026, 8, 6).date())
    assert out["expiry"] == "2026-08-11"
