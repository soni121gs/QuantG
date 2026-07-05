"""IMD-03 — bounded importer planner + ingest (fake gateway, no network)."""
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from core.expired_option_resolver import ExpiredOptionResolver  # noqa: E402
from core.options_minute_store import OptionsMinuteStore  # noqa: E402
from scripts.options_1m_ingest_upstox import ingest, plan_contract_days  # noqa: E402


def _contract(strike, opt, expiry="2025-01-09"):
    return {
        "expired_instrument_key": f"NSE_FO|{int(strike)}{opt}|{expiry}",
        "trading_symbol": f"NIFTY {int(strike)} {opt}",
        "strike_price": float(strike), "instrument_type": opt,
        "lot_size": 75, "expiry": expiry,
    }


def _resolver():
    contracts = [_contract(s, o) for s in range(24000, 24500, 50) for o in ("CE", "PE")]
    return ExpiredOptionResolver(lambda k, e: contracts)


def _plan(strikes_around_atm=1):
    return plan_contract_days(
        ["NIFTY"], ["2025-01-09"],
        spot_fn=lambda u, d: 24215.0,
        expiry_fn=lambda u, d: "2025-01-09",
        resolver=_resolver(),
        strikes_around_atm=strikes_around_atm,
    )


def test_plan_counts_atm_window_ce_and_pe():
    plan = _plan(strikes_around_atm=1)
    # ATM 24200 ± 1 strike (50 step) = 24150/24200/24250 × CE/PE = 6
    assert plan["planned_count"] == 6
    strikes = sorted({p["strike"] for p in plan["plans"]})
    assert strikes == [24150.0, 24200.0, 24250.0]


def test_plan_skips_unsupported_underlying():
    plan = plan_contract_days(
        ["SENSEX"], ["2025-01-09"], spot_fn=lambda u, d: 80000.0,
        expiry_fn=lambda u, d: "2025-01-09", resolver=_resolver(), strikes_around_atm=1,
    )
    assert plan["planned_count"] == 0
    assert plan["skipped"][0]["reason"] == "UNSUPPORTED_OR_BLOCKED"


def test_plan_skips_missing_spot():
    plan = plan_contract_days(
        ["NIFTY"], ["2025-01-09"], spot_fn=lambda u, d: None,
        expiry_fn=lambda u, d: "2025-01-09", resolver=_resolver(), strikes_around_atm=1,
    )
    assert plan["planned_count"] == 0


class _FakeGateway:
    def __init__(self):
        self.calls = 0

    def get_expired_historical_candles_v2(self, key, *, unit, interval, from_date, to_date):
        self.calls += 1
        return [
            {"date": f"{from_date} 09:15", "open": 100, "high": 105, "low": 98, "close": 102, "volume": 1000, "oi": 250000},
            {"date": f"{from_date} 09:16", "open": 102, "high": 106, "low": 101, "close": 104, "volume": 1200, "oi": 250500},
        ]


@pytest.fixture()
def store():
    root = os.path.join(os.path.dirname(__file__), "_tmp_ingest_store")
    shutil.rmtree(root, ignore_errors=True)
    yield OptionsMinuteStore(root=root)
    shutil.rmtree(root, ignore_errors=True)


def test_ingest_writes_and_is_idempotent(store):
    plan = _plan(strikes_around_atm=0)  # just ATM CE+PE = 2 contracts
    gw = _FakeGateway()
    s1 = ingest(plan["plans"], gw, store, fetched_at="2026-07-06T00:00:00+05:30")
    assert s1["fetched"] == 2 and s1["rows"] == 4
    assert gw.calls == 2

    # re-run: clean contract-days are skipped, gateway not hit again
    s2 = ingest(plan["plans"], gw, store, fetched_at="2026-07-06T00:00:00+05:30")
    assert s2["fetched"] == 0 and s2["skipped_clean"] == 2
    assert gw.calls == 2


def test_ingest_force_refetches(store):
    plan = _plan(strikes_around_atm=0)
    gw = _FakeGateway()
    ingest(plan["plans"], gw, store, fetched_at="x")
    ingest(plan["plans"], gw, store, force=True, fetched_at="x")
    assert gw.calls == 4


def test_ingest_empty_candles_counted(store):
    class _Empty(_FakeGateway):
        def get_expired_historical_candles_v2(self, *a, **k):
            return []

    plan = _plan(strikes_around_atm=0)
    s = ingest(plan["plans"], _Empty(), store, fetched_at="x")
    assert s["fetched"] == 0 and s["empty"] == 2
