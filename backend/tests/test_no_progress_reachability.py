"""The "no progress" bar must be reachable before it is applied.

`no_progress_exit` cut 30 of the 39 post-re-cut spread exits (77%) at an average of
-Rs127 a trade. Its 8%-of-credit bar was calibrated against the peak distribution of
trades measured over their WHOLE hold (winners median-peak 29%, dead trades ~0.3%),
then applied inside a flat 20-minute window. Those are different questions.

Theta can only return `held / (dte x session)` of a position's remaining time value.
In 20 minutes that is ~5.2% at 0-1 DTE but only 1.30% at 4 DTE and 0.87% at 6 DTE —
while the bar asks for 8%. Measured on the 30 real trades it closed: entries sat at
DTE 4 (n=10), 6 (n=9), 25 (n=5); mean peak 3.40% of credit; ZERO of 30 ever cleared
8%. The rule was not separating dead trades from live ones — it was cutting anything
that had not been directionally lucky in its first 20 minutes.

Same defect class as §21.5 / §22.3: a threshold measured under one regime applied to
another. NOTE: holding these trades longer is NOT claimed to make them profitable —
they are simply judged later, on evidence that could exist.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.dynamic_exit as de
from core.dynamic_exit import no_progress_exit

SESSION = 385.0
CREDIT, QTY = 10.0, 65          # NIFTY-ish: credit_money = Rs650, 8% bar = Rs52


def _call(**kw):
    base = dict(peak_pnl=0.0, held_minutes=20.0, net_credit=CREDIT, qty=QTY,
                session_minutes=SESSION)
    base.update(kw)
    return no_progress_exit(**base)


def test_far_dte_is_not_judged_in_a_twenty_minute_window():
    """At 6 DTE only 0.87% of time value can have decayed in 20 min; the bar is 8%."""
    assert _call(dte_days=6.0) is None
    assert _call(dte_days=4.0) is None


def test_it_still_fires_once_decay_has_had_its_chance():
    """Same 6-DTE trade, judged after enough of its life has actually elapsed."""
    assert _call(dte_days=6.0, held_minutes=6.0 * SESSION * 0.5) == "spread-no-progress"


def test_near_expiry_is_still_cut_promptly():
    """0-1 DTE supplies ~5.2% in 20 min, so the bar is nearly reachable there and a
    genuinely dead scalp is still cut quickly — the rule keeps its original purpose."""
    assert _call(dte_days=0.5, held_minutes=60.0) == "spread-no-progress"


def test_a_live_trade_is_never_cut():
    """Peak well clear of the bar → no cut, at any DTE."""
    assert _call(dte_days=6.0, held_minutes=2000.0, peak_pnl=500.0) is None
    assert _call(dte_days=0.5, held_minutes=60.0, peak_pnl=500.0) is None


def test_window_floor_still_applies():
    """Nothing is judged before NO_PROGRESS_MINUTES regardless of DTE."""
    assert _call(dte_days=0.1, held_minutes=1.0) is None


def test_missing_dte_preserves_the_old_behaviour():
    """A position with no expiry recorded must not silently become un-cuttable."""
    assert _call(dte_days=None) == "spread-no-progress"


def test_gate_is_env_reversible(monkeypatch):
    monkeypatch.setattr(de, "NO_PROGRESS_REQUIRE_REACHABLE", False)
    assert _call(dte_days=6.0) == "spread-no-progress"


def test_disabled_flag_is_still_a_noop(monkeypatch):
    monkeypatch.setattr(de, "NO_PROGRESS_ENABLED", False)
    assert _call(dte_days=0.5, held_minutes=300.0) is None


def test_the_arithmetic_that_proves_the_bar_was_unreachable():
    """Pins the measurement, so nobody re-flattens the window without seeing it."""
    bar = de.NO_PROGRESS_PEAK_FRAC                      # 0.08 of credit
    for dte, available in ((1, 20 / (1 * SESSION)), (4, 20 / (4 * SESSION)),
                           (6, 20 / (6 * SESSION))):
        if dte > 1:
            assert available < bar, (dte, available, bar)
    # 4 DTE: 1.30%, 6 DTE: 0.87% — both far under the 8% asked for
    assert round(20 / (4 * SESSION) * 100, 2) == 1.30
    assert round(20 / (6 * SESSION) * 100, 2) == 0.87
