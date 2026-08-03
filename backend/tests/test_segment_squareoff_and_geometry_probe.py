"""Two guards from the 2026-08-03 loss-structure audit.

1. SEGMENT-AWARE SQUARE-OFF. The SEBI change split the derivatives closes (NSE 15:40,
   BSE 15:30) but the spread square-off stayed a SINGLE global minute derived from the
   NSE close — 15:35, five minutes AFTER BSE had shut. A SENSEX spread would have been
   closed against a stale mark in paper and rejected in live. The old 15:25 value hid
   it because it preceded both closes, so the bug shipped and armed itself on the day
   the session changed.

2. GEOMETRY vs REALISED WIN RATE. `static.reward_risk_geometry` compares break-even WR
   to a FIXED 0.75 constant, which is exactly why it stayed silent: every live seller
   sat at tp 0.25 / sl 0.60 = 70.6% break-even, just under the alarm, while realising
   50-62%. A fixed threshold cannot know whether 71% is achievable; only the
   strategy's own record can.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import session_times as st


# ── 1. segment-aware square-off ──────────────────────────────────────────────

def test_bse_squareoff_precedes_the_bse_close():
    bse = st.spread_squareoff_minute_for("BSE_FO")
    assert bse < st.BSE_FO_CLOSE_MINUTE, (st.hhmm_str(bse), st.hhmm_str(st.BSE_FO_CLOSE_MINUTE))


def test_nse_squareoff_precedes_the_nse_close():
    nse = st.spread_squareoff_minute_for("NSE_FO")
    assert nse < st.NSE_FO_CLOSE_MINUTE


def test_the_global_squareoff_would_have_been_late_for_bse():
    """The exact regression: the NSE-derived global minute is past the BSE close."""
    assert st.SPREAD_SQUAREOFF_MINUTE > st.BSE_FO_CLOSE_MINUTE
    assert st.spread_squareoff_minute_for("BSE_FO") < st.BSE_FO_CLOSE_MINUTE


def test_segment_for_underlying_maps_the_bse_names():
    assert st.segment_for_underlying("SENSEX") == "BSE_FO"
    assert st.segment_for_underlying("sensex") == "BSE_FO"
    assert st.segment_for_underlying("BANKEX") == "BSE_FO"
    assert st.segment_for_underlying("NIFTY") == "NSE_FO"
    assert st.segment_for_underlying("BANKNIFTY") == "NSE_FO"
    assert st.segment_for_underlying(None) == "NSE_FO"
    assert st.segment_for_underlying("") == "NSE_FO"


def test_monitor_squareoff_is_segment_aware():
    """The monitor must ask per underlying, not read one global constant."""
    src = (Path(__file__).resolve().parents[1] / "position_monitor.py").read_text(encoding="utf-8")
    assert "def _spread_squareoff_due(underlying" in src
    assert "_spread_squareoff_due(_sq_underlying)" in src
    assert "spread_squareoff_minute_for" in src


def test_squareoff_reason_carries_the_real_minute():
    """The literal "1525" kept appearing after the square-off moved, which made every
    exit-timing question unanswerable from the ledger."""
    src = (Path(__file__).resolve().parents[1] / "position_monitor.py").read_text(encoding="utf-8")
    assert 'intraday-squareoff-1525' not in src
    assert 'f"intraday-squareoff-{_sq_at.replace' in src


def test_backstop_sweep_runs_a_separate_bse_pass():
    src = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    assert 'segment="BSE_FO"' in src and 'segment="NSE_FO"' in src
    assert "_bse_spread_squareoff_done_date" in src


# ── 2. geometry vs realised win rate ─────────────────────────────────────────

def _be(tp, sl):
    return sl / (sl + tp)


def test_the_deployed_geometry_needs_more_wins_than_it_gets():
    """Pins the arithmetic of the finding: tp 0.25 / sl 0.60 needs ~71%."""
    assert round(_be(0.25, 0.60), 3) == 0.706


def test_lowering_only_the_target_makes_the_geometry_harder():
    """The logic error behind the 2026-07-30 retune: TP was halved (0.50 -> 0.25) while
    SL fell by only a third (0.90 -> 0.60), so the break-even win rate ROSE."""
    before = _be(0.50, 0.90)
    after = _be(0.25, 0.60)
    assert after > before
    assert round(before, 3) == 0.643 and round(after, 3) == 0.706


def test_the_static_probe_threshold_sits_above_the_deployed_geometry():
    """Why the existing probe never fired — 0.706 < 0.75."""
    import os
    warn = float(os.environ.get("HERMES_GEOMETRY_BE_WR_WARN", "0.75"))
    assert _be(0.25, 0.60) < warn


def test_probe_is_registered_and_scoped():
    import core.hermes_diagnostics.probes_strategy as ps
    src = Path(ps.__file__).read_text(encoding="utf-8")
    assert '@register("strategy.geometry_vs_realized_wr"' in src
    # must respect the geometry epoch and a minimum sample
    assert "geometry_changed_at" in src
    assert "_GEOM_WR_MIN_SAMPLE" in src
    # hold-to-expiry omits tp/sl by design — it must be skipped, not defaulted
    assert "if tp is None or sl is None:" in src


def test_segment_close_probe_is_registered():
    import core.hermes_diagnostics.probes_execution as pe
    src = Path(pe.__file__).read_text(encoding="utf-8")
    assert '@register("exec.squareoff_after_segment_close"' in src
    assert "segment_for_underlying" in src and "close_minute_for" in src
