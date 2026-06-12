from datetime import datetime, timezone

import pytest

from upstox_trading_quality import (
    feed_health_status,
    lookup_instrument_guard,
    normalize_instrument_row,
    option_entry_quality_score,
)


def test_normalize_instrument_requires_instrument_key_not_exchange_token():
    row = {
        "exchange_token": "12345",
        "trading_symbol": "NIFTY26JUN25000CE",
        "instrument_type": "CE",
    }
    assert normalize_instrument_row(row, source="nse") is None

    valid = normalize_instrument_row(
        {**row, "instrument_key": "NSE_FO|12345", "exchange": "NSE", "segment": "NSE_FO"},
        source="nse",
    )
    assert valid["instrument_key"] == "NSE_FO|12345"
    assert valid["exchange_token"] == "12345"


def test_option_quality_penalizes_stale_wide_spread_contract():
    fresh = datetime.now(timezone.utc).isoformat()
    good = option_entry_quality_score(
        {"strike": 25000, "spot": 24980, "ltp": 120},
        quote={"bid": 119, "ask": 121, "volume": 25000, "oi": 100000, "timestamp": fresh},
        pcr=0.9,
    )
    bad = option_entry_quality_score(
        {"strike": 26000, "spot": 24980, "ltp": 0},
        quote={"bid": 10, "ask": 30, "volume": 0, "oi": 0, "timestamp": "2020-01-01T00:00:00+00:00"},
        pcr=3.0,
    )
    assert good["score"] > bad["score"]
    assert "QUOTE_STALE" in bad["warnings"]
    assert "WIDE_SPREAD" in bad["warnings"]


def test_option_quality_reports_no_depth_separately_from_block():
    scored = option_entry_quality_score(
        {"strike": 25000, "spot": 24980, "ltp": 120},
        quote={"ltp": 120, "timestamp": datetime.now(timezone.utc).isoformat()},
        pcr=0.9,
    )
    assert scored["readiness"] == "NO_DEPTH"
    assert scored["score"] >= 45


def test_feed_health_marks_missing_snapshot_unready():
    status = {
        "connected": True,
        "feed_status": {
            "connected": True,
            "snapshot_received": False,
            "subscribed_count": 1,
        },
    }
    health = feed_health_status(status, {})
    assert health["readiness"] == "DEGRADED"
    assert health["snapshot_received"] is False


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    async def find_one(self, query, *_args, **_kwargs):
        return self.rows.get(query.get("_id"))


class _Db:
    def __init__(self):
        self.upstox_instruments = _Collection({"NSE_FO|1": {"instrument_key": "NSE_FO|1"}})
        self.upstox_suspended_instruments = _Collection({"NSE_FO|2": {"instrument_key": "NSE_FO|2"}})


@pytest.mark.asyncio
async def test_lookup_instrument_guard_blocks_suspended():
    db = _Db()
    assert (await lookup_instrument_guard(db, "NSE_FO|1"))["ok"] is True
    blocked = await lookup_instrument_guard(db, "NSE_FO|2")
    assert blocked["ok"] is False
    assert blocked["reason_code"] == "INSTRUMENT_SUSPENDED"
    invalid = await lookup_instrument_guard(db, "12345")
    assert invalid["reason_code"] == "INSTRUMENT_KEY_INVALID"
