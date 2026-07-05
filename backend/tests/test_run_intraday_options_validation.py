"""IMD-08 — orchestration helpers (compile_signal_fn / build_scorecard / empty-data)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.intraday_options_oos import CANDIDATE_EDGE, INSUFFICIENT_DATA, NO_EDGE_NEGATIVE  # noqa: E402
from scripts.run_intraday_options_validation import (  # noqa: E402
    build_scorecard,
    compile_signal_fn,
    validate_strategy,
)

CODE = """
def run(data):
    d = data[-1]
    if float(d.get('close') or 0) > 100:
        return [{'date': d['date'], 'action': 'BUY', 'direction': 'CE', 'setup_type': 'x'}]
    return []
"""


def test_compile_signal_fn_emits_last_signal():
    fn = compile_signal_fn(CODE)
    assert fn([{"date": "d", "close": 90}]) is None
    sig = fn([{"date": "d", "close": 150}])
    assert sig and sig["direction"] == "CE"


def test_compile_signal_fn_swallows_errors():
    fn = compile_signal_fn("def run(data):\n    raise ValueError('boom')")
    assert fn([{"date": "d"}]) is None


def test_validate_strategy_empty_data_is_insufficient():
    r = validate_strategy(
        "QG-O5", CODE, underlying="NIFTY", structure="single_leg",
        days=["2025-01-06", "2025-01-07"],
        underlying_minutes_fn=lambda u, d: [],   # no index minutes yet
        chain_at_fn=lambda u, d: (lambda ts: {}),
        option_series_fn=lambda u, d: {},
    )
    assert r["verdict"] in (INSUFFICIENT_DATA, "DATA_QUALITY_FAIL")
    assert r["trades"] == 0


def test_build_scorecard_ranks_candidates_first():
    results = [
        {"strategy": "A", "verdict": NO_EDGE_NEGATIVE, "overall": {"expectancy": -5}},
        {"strategy": "B", "verdict": CANDIDATE_EDGE, "overall": {"expectancy": 10}},
    ]
    sc = build_scorecard(results)
    assert sc["results"][0]["strategy"] == "B"
    assert sc["candidates"] == ["B"]
    assert sc["verdict_counts"][CANDIDATE_EDGE] == 1
