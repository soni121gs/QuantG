"""The market regime must be refreshed BEFORE any signal gate.

2026-08-03: `update_regime` sat below the `not signals` / `not last_sig` /
duplicate-signal / low-confidence `continue`s in the strategy-runner loop, so an
underlying's regime was only recomputed when one of its strategies happened to emit a
VALID signal.

BANKNIFTY's only live strategy is the trend rider, which needs a fresh 30-bar breakout.
It fired at 10:05 IST and then went quiet, so `market_regime_state.BANKNIFTY` stayed
frozen at its 10:05 value for the rest of the session (computed_at 04:35 UTC, three
hours stale) while NIFTY/SENSEX — whose sellers signal every tick — refreshed every
~2 minutes.

That is not cosmetic. The same block detects a mid-session regime FLIP and tightens
against-regime positions, and the CRASH/MELTUP entry blocks read the cached label. Both
therefore depended on the underlying's strategies being chatty: a genuine intraday crash
on a quiet underlying would not have tightened its open positions.

The regime is a property of the MARKET, not of whether a strategy liked it.

This is a source-order test on purpose — the runner loop is a long async function with
DB/broker dependencies, and the invariant that matters is positional. Same approach as
the §25.4b falsy-return guard.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SRC = (Path(__file__).resolve().parents[1] / "strategy_runner.py").read_text(encoding="utf-8")


def _pos(pattern: str) -> int:
    m = re.search(pattern, SRC)
    assert m, f"pattern not found in strategy_runner.py: {pattern}"
    return m.start()


def test_update_regime_runs_before_the_no_signals_gate():
    assert _pos(r"await update_regime\(") < _pos(r"if not signals:")


def test_update_regime_runs_before_the_stale_signal_gate():
    assert _pos(r"await update_regime\(") < _pos(r"if not last_sig:")


def test_update_regime_runs_before_the_duplicate_signal_gate():
    assert _pos(r"await update_regime\(") < _pos(r"if last_sig_date and last_sig_date == last_fired_date:")


def test_regime_flip_detection_runs_before_the_signal_gates():
    """The flip handler tightens against-regime positions — it must not be reachable
    only when a strategy happens to like the setup."""
    assert _pos(r"_tighten_positions_on_regime_flip") < _pos(r"if not signals:")


def test_update_regime_is_called_exactly_once_in_the_loop():
    """Guards against the refactor leaving the old call behind and double-writing."""
    assert len(re.findall(r"await update_regime\(", SRC)) == 1


def test_regime_is_computed_from_the_candles_already_fetched():
    """It must not add a broker round-trip — `data` is in hand at that point."""
    assert _pos(r"data = _enrich_tod_ratios\(data\)") < _pos(r"await update_regime\(")
