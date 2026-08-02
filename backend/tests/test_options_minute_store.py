"""IMD-03/IMD-05 — options 1-minute store write + read (filesystem-backed)."""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from core.options_minute_schema import (  # noqa: E402
    OptionContractRef,
    build_manifest,
    normalize_candles,
)
from core.options_minute_store import OptionsMinuteStore, expected_minutes  # noqa: E402

REF = OptionContractRef(
    underlying="NIFTY", expiry="2025-01-09", strike=24200.0, option_type="CE",
    instrument_key="NSE_FO|43567", expired_instrument_key="NSE_FO|43567|09-01-2025",
)


def _rows(n=3, opt="CE", strike=24200.0):
    ref = OptionContractRef("NIFTY", "2025-01-09", strike, opt, "NSE_FO|x", f"NSE_FO|{int(strike)}{opt}|exp")
    raw = [[f"2025-01-09T09:{15+i:02d}:00+05:30", 100 + i, 105 + i, 98 + i, 102 + i, 1000 + i, 250000 + i] for i in range(n)]
    return normalize_candles(raw, ref)


def _manifest(res, ref=REF):
    return build_manifest(res.candles, ref, source="upstox", from_date="2025-01-09",
                          to_date="2025-01-09", fetched_at="2026-07-06T00:00:00+05:30", flags=res.flags)


@pytest.fixture()
def store():
    root = os.path.join(os.path.dirname(__file__), "_tmp_opt1m_store")
    shutil.rmtree(root, ignore_errors=True)
    yield OptionsMinuteStore(root=root)
    shutil.rmtree(root, ignore_errors=True)


def test_write_then_read_roundtrip(store):
    res = _rows()
    store.write_contract_day(res.candles, _manifest(res))
    got = store.get_option_minutes("upstox", "NIFTY", "2025-01-09", "2025-01-09", 24200.0, "CE")
    assert len(got) == 3
    assert got[0]["open"] == 100.0 and got[0]["volume"] == 1000
    assert got[0]["underlying"] == "NIFTY" and got[0]["option_type"] == "CE"


def test_missing_contract_returns_empty_not_guess(store):
    assert store.get_option_minutes("upstox", "NIFTY", "2025-01-09", "2025-01-09", 99999.0, "CE") == []


def test_is_clean_idempotency(store):
    res = _rows()
    assert store.is_clean("upstox", "NIFTY", "2025-01-09", "2025-01-09", 24200.0, "CE") is False
    store.write_contract_day(res.candles, _manifest(res))
    assert store.is_clean("upstox", "NIFTY", "2025-01-09", "2025-01-09", 24200.0, "CE") is True


def test_chain_at_time_no_lookahead(store):
    ce = _rows(opt="CE", strike=24200.0)
    pe = _rows(opt="PE", strike=24200.0)
    store.write_contract_day(ce.candles, _manifest(ce, ce.candles[0] and OptionContractRef("NIFTY", "2025-01-09", 24200.0, "CE", "k", "ek")))
    store.write_contract_day(pe.candles, _manifest(pe, OptionContractRef("NIFTY", "2025-01-09", 24200.0, "PE", "k", "ek")))
    # as of 09:16 only two bars (09:15, 09:16) exist; last should be 09:16
    chain = store.get_chain_at_time("upstox", "NIFTY", "2025-01-09", "2025-01-09T09:16:00+05:30")
    node = chain["2025-01-09"][24200.0]
    assert node["CE"]["timestamp_ist"] == "2025-01-09T09:16:00+05:30"
    assert "PE" in node
    # a future bar (09:17) must not leak into the 09:16 snapshot
    assert node["CE"]["close"] == 103.0


def test_missing_minutes_report(store):
    res = _rows(n=3)
    store.write_contract_day(res.candles, _manifest(res))
    missing = store.missing_minutes("upstox", "NIFTY", "2025-01-09", "2025-01-09", 24200.0, "CE")
    # 385 bars from 2026-08-03 (NSE F&O 09:15-15:40); was 375 (09:15-15:30).
    assert len(expected_minutes("2025-01-09")) == 385
    assert len(missing) == 385 - 3
    assert "2025-01-09T09:15:00+05:30" not in missing
    assert "2025-01-09T09:18:00+05:30" in missing


def test_coverage_summary(store):
    ce = _rows(opt="CE", strike=24200.0)
    pe = _rows(opt="PE", strike=24100.0)
    store.write_contract_day(ce.candles, _manifest(ce, OptionContractRef("NIFTY", "2025-01-09", 24200.0, "CE", "k", "ek")))
    store.write_contract_day(pe.candles, _manifest(pe, OptionContractRef("NIFTY", "2025-01-09", 24100.0, "PE", "k", "ek")))
    cov = store.coverage("upstox", "NIFTY")
    assert cov["total_contract_days"] == 2
    assert cov["total_rows"] == 6
    assert cov["underlyings"] == ["NIFTY"]
    assert cov["days"] == ["2025-01-09"]


def test_trading_days(store):
    res = _rows()
    store.write_contract_day(res.candles, _manifest(res))
    assert store.trading_days("upstox", "NIFTY") == ["2025-01-09"]
