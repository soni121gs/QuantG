"""IMD-01 — canonical 1-minute options schema + normalization (pure logic)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.options_minute_schema import (  # noqa: E402
    DQ_MISSING_OI,
    DQ_ZERO_VOLUME,
    OptionContractRef,
    REASON_HIGH_LT_LOW,
    REASON_NEGATIVE_PRICE,
    REASON_NON_NUMERIC,
    REASON_OHLC_OUT_OF_RANGE,
    REASON_ROW_TOO_SHORT,
    build_manifest,
    manifest_checksum,
    normalize_candle,
    normalize_candles,
    normalize_timestamp,
)

REF = OptionContractRef(
    underlying="NIFTY",
    expiry="2025-01-09",
    strike=24200.0,
    option_type="CE",
    instrument_key="NSE_FO|43567",
    expired_instrument_key="NSE_FO|43567|09-01-2025",
)


def _raw(ts="2025-01-09T09:15:00+05:30", o=100.0, h=105.0, low=98.0, c=102.0, vol=1500, oi=250000):
    return [ts, o, h, low, c, vol, oi]


# ---- happy path -------------------------------------------------------------
def test_normalize_candle_maps_all_fields():
    row = normalize_candle(_raw(), REF, source="upstox", fetched_at="2026-07-06T00:00:00+05:30")
    assert row["timestamp_ist"] == "2025-01-09T09:15:00+05:30"
    assert row["underlying"] == "NIFTY"
    assert row["strike"] == 24200.0
    assert row["option_type"] == "CE"
    assert row["expired_instrument_key"] == "NSE_FO|43567|09-01-2025"
    assert row["open"] == 100.0 and row["close"] == 102.0
    assert row["volume"] == 1500 and row["open_interest"] == 250000.0
    assert row["source"] == "upstox"
    assert len(row["checksum"]) == 64
    assert "_has_oi" not in normalize_candles([_raw()], REF).candles[0]


def test_timestamp_without_offset_assumed_ist():
    assert normalize_timestamp("2025-01-09T09:15:00") == "2025-01-09T09:15:00+05:30"


def test_timestamp_utc_converted_to_ist():
    assert normalize_timestamp("2025-01-09T03:45:00Z") == "2025-01-09T09:15:00+05:30"


# ---- bad rows fail with explicit reasons ------------------------------------
def test_row_too_short():
    res = normalize_candles([["2025-01-09T09:15:00+05:30", 1, 2]], REF)
    assert res.candles == []
    assert res.errors[0]["reason"] == REASON_ROW_TOO_SHORT


def test_non_numeric_price():
    res = normalize_candles([_raw(o="abc")], REF)
    assert res.errors[0]["reason"] == REASON_NON_NUMERIC


def test_negative_price():
    res = normalize_candles([_raw(low=-1.0)], REF)
    assert res.errors[0]["reason"] in (REASON_NEGATIVE_PRICE, REASON_HIGH_LT_LOW, REASON_OHLC_OUT_OF_RANGE)


def test_high_less_than_low():
    res = normalize_candles([_raw(h=90.0, low=95.0)], REF)
    assert res.errors[0]["reason"] == REASON_HIGH_LT_LOW


def test_close_outside_high_low():
    res = normalize_candles([_raw(o=100, h=105, low=98, c=110)], REF)
    assert res.errors[0]["reason"] == REASON_OHLC_OUT_OF_RANGE


def test_good_and_bad_rows_partition():
    res = normalize_candles([_raw(), _raw(h=1, low=9), _raw(ts="2025-01-09T09:16:00+05:30")], REF)
    assert len(res.candles) == 2
    assert len(res.errors) == 1


# ---- data-quality flags (parseable but suspicious) --------------------------
def test_zero_volume_flagged_not_rejected():
    res = normalize_candles([_raw(vol=0)], REF)
    assert len(res.candles) == 1
    assert res.flags[DQ_ZERO_VOLUME] == 1


def test_missing_oi_flagged():
    res = normalize_candles([["2025-01-09T09:15:00+05:30", 100, 105, 98, 102, 1500]], REF)
    assert len(res.candles) == 1
    assert res.flags[DQ_MISSING_OI] == 1
    assert res.candles[0]["open_interest"] == 0.0


# ---- checksums --------------------------------------------------------------
def test_row_checksum_stable_and_content_addressed():
    a = normalize_candle(_raw(), REF)
    b = normalize_candle(_raw(), REF)
    c = normalize_candle(_raw(c=103.0), REF)
    assert a["checksum"] == b["checksum"]
    assert a["checksum"] != c["checksum"]


def test_manifest_checksum_order_independent():
    rows = normalize_candles(
        [_raw(ts="2025-01-09T09:15:00+05:30"), _raw(ts="2025-01-09T09:16:00+05:30")], REF
    ).candles
    assert manifest_checksum(rows) == manifest_checksum(list(reversed(rows)))


def test_manifest_shape():
    res = normalize_candles(
        [_raw(ts="2025-01-09T09:15:00+05:30"), _raw(ts="2025-01-09T09:16:00+05:30", vol=0)], REF
    )
    man = build_manifest(
        res.candles, REF, source="upstox",
        from_date="2025-01-09", to_date="2025-01-09",
        fetched_at="2026-07-06T00:00:00+05:30", flags=res.flags,
    )
    assert man["row_count"] == 2
    assert man["first_timestamp"] == "2025-01-09T09:15:00+05:30"
    assert man["last_timestamp"] == "2025-01-09T09:16:00+05:30"
    assert man["underlying"] == "NIFTY" and man["option_type"] == "CE"
    assert man["data_quality_flags"][DQ_ZERO_VOLUME] == 1
    assert len(man["checksum"]) == 64


def test_empty_manifest():
    man = build_manifest([], REF, source="upstox", from_date="2025-01-09", to_date="2025-01-09", fetched_at="x")
    assert man["row_count"] == 0
    assert man["first_timestamp"] is None and man["last_timestamp"] is None
