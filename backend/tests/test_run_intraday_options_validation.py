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


def test_store_backed_providers_produce_trades():
    import shutil
    from core.options_minute_schema import OptionContractRef, build_manifest, normalize_candles
    from core.options_minute_store import OptionsMinuteStore
    from core.index_minute_store import IndexMinuteStore

    oroot = os.path.join(os.path.dirname(__file__), "_tmp_e2e_opt")
    iroot = os.path.join(os.path.dirname(__file__), "_tmp_e2e_idx")
    shutil.rmtree(oroot, ignore_errors=True)
    shutil.rmtree(iroot, ignore_errors=True)
    try:
        ostore = OptionsMinuteStore(root=oroot)
        istore = IndexMinuteStore(root=iroot)
        date = "2025-01-09"
        # index: 3 minutes, spot ~24215 (ATM 24200)
        istore.write_day("NIFTY", date, [
            {"timestamp_ist": f"{date}T09:{15 + k:02d}:00+05:30", "underlying": "NIFTY",
             "open": 24215, "high": 24215, "low": 24215, "close": 24215, "volume": 0} for k in range(3)])
        # option CE 24200: entry 100 -> jumps to target
        ref = OptionContractRef("NIFTY", date, 24200.0, "CE", "NSE_FO|CE24200", "NSE_FO|CE24200|exp")
        raw = [[f"{date}T09:{15 + k:02d}:00+05:30", *ohlc, 1000, 250000]
               for k, ohlc in enumerate([(100, 100, 100, 100), (150, 210, 95, 205), (205, 210, 200, 205)])]
        res = normalize_candles(raw, ref)
        ostore.write_contract_day(res.candles, build_manifest(
            res.candles, ref, source="upstox", from_date=date, to_date=date, fetched_at="x", flags=res.flags))

        code = ("def run(data):\n"
                "    d = data[-1]\n"
                "    if len(data) == 1:\n"
                "        return [{'date': d['date'], 'action': 'BUY', 'direction': 'CE',"
                " 'structure': 'single_leg', 'setup_type': 't', 'target_R': 1.0, 'initial_stop_R': 0.5}]\n"
                "    return []\n")
        r = validate_strategy(
            "QG-TEST", code, underlying="NIFTY", structure="single_leg", days=[date],
            underlying_minutes_fn=lambda u, d: istore.get_minutes(u, d),
            chain_at_fn=lambda u, d: (lambda ts: ostore.get_chain_at_time("upstox", u, d, ts)),
            option_series_fn=lambda u, d: ostore.all_series_for_day("upstox", u, d),
        )
        assert r["trades"] == 1
        assert r["overall"]["net_pnl"] > 0  # bought 100, target 200
    finally:
        shutil.rmtree(oroot, ignore_errors=True)
        shutil.rmtree(iroot, ignore_errors=True)


def test_build_scorecard_ranks_candidates_first():
    results = [
        {"strategy": "A", "verdict": NO_EDGE_NEGATIVE, "overall": {"expectancy": -5}},
        {"strategy": "B", "verdict": CANDIDATE_EDGE, "overall": {"expectancy": 10}},
    ]
    sc = build_scorecard(results)
    assert sc["results"][0]["strategy"] == "B"
    assert sc["candidates"] == ["B"]
    assert sc["verdict_counts"][CANDIDATE_EDGE] == 1
