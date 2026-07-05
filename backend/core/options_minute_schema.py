"""IMD-01: canonical schema + normalization for the 1-minute options history store.

Pure logic — no broker calls, no filesystem writes. The importer (IMD-03) and the
live capture loop (IMD-04) both push raw Upstox candle arrays through here so every
row that lands in ``data/options_1m/`` has the same shape, the same validation, and
a deterministic per-row checksum (dedup + integrity). Manifests summarize a fetch
window with their own deterministic checksum so a re-fetch of clean data is a no-op.

Upstox raw candle array shape (v2 expired-instruments / v3 historical):
    [timestamp, open, high, low, close, volume, open_interest]
timestamp is an ISO-8601 string carrying the IST offset, e.g. ``2025-01-09T09:15:00+05:30``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

IST = timezone(timedelta(hours=5, minutes=30))

CANDLE_FIELDS = (
    "timestamp_ist", "instrument_key", "expired_instrument_key", "underlying",
    "expiry", "strike", "option_type", "open", "high", "low", "close",
    "volume", "open_interest", "source", "fetched_at", "checksum",
)

# Hard-fail reasons — a row carrying one of these is rejected, never stored.
REASON_ROW_TOO_SHORT = "ROW_TOO_SHORT"
REASON_BAD_TIMESTAMP = "BAD_TIMESTAMP"
REASON_NON_NUMERIC = "NON_NUMERIC_PRICE"
REASON_NEGATIVE_PRICE = "NEGATIVE_PRICE"
REASON_HIGH_LT_LOW = "HIGH_LT_LOW"
REASON_OHLC_OUT_OF_RANGE = "OHLC_OUT_OF_RANGE"
REASON_BAD_OPTION_TYPE = "BAD_OPTION_TYPE"

# Collection-level data-quality flags — parseable but suspicious; surfaced on the
# manifest, they do NOT reject the row (a 0-volume option minute is legitimate).
DQ_ZERO_VOLUME = "ZERO_VOLUME_BARS"
DQ_DUPLICATE_TS = "DUPLICATE_TIMESTAMPS"
DQ_NON_MONOTONIC = "NON_MONOTONIC_TIMESTAMPS"
DQ_MISSING_OI = "MISSING_OPEN_INTEREST"


class CandleError(ValueError):
    """A single raw candle failed structural/consistency validation."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class OptionContractRef:
    """The identity a raw candle array lacks — supplied by the caller (resolver)."""

    underlying: str
    expiry: str            # YYYY-MM-DD
    strike: float
    option_type: str       # CE / PE
    instrument_key: str
    expired_instrument_key: str


@dataclass
class NormalizeResult:
    candles: List[Dict[str, Any]]
    errors: List[Dict[str, Any]]
    flags: Dict[str, int]


def normalize_timestamp(raw: Any) -> str:
    """Return a canonical IST ISO-8601 string, raising CandleError on junk."""
    text = str(raw).strip()
    if not text:
        raise CandleError(REASON_BAD_TIMESTAMP, "empty")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise CandleError(REASON_BAD_TIMESTAMP, text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).isoformat()


def _num(raw: Any, reason: str) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise CandleError(reason, str(raw))
    if val != val:  # NaN
        raise CandleError(reason, "nan")
    return val


def _row_checksum(row: Dict[str, Any]) -> str:
    parts = (
        row["timestamp_ist"], row["expired_instrument_key"],
        f"{row['open']:.4f}", f"{row['high']:.4f}",
        f"{row['low']:.4f}", f"{row['close']:.4f}",
        str(row["volume"]), f"{row['open_interest']:.4f}",
    )
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def normalize_candle(
    raw: Sequence[Any],
    ref: OptionContractRef,
    *,
    source: str = "upstox",
    fetched_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize one raw Upstox candle array into the canonical row.

    Raises CandleError (with .reason) on any structurally- or internally-invalid row.
    """
    if raw is None or len(raw) < 5:
        raise CandleError(REASON_ROW_TOO_SHORT, f"len={0 if raw is None else len(raw)}")

    option_type = str(ref.option_type).upper()
    if option_type not in ("CE", "PE"):
        raise CandleError(REASON_BAD_OPTION_TYPE, ref.option_type)

    ts = normalize_timestamp(raw[0])
    o = _num(raw[1], REASON_NON_NUMERIC)
    h = _num(raw[2], REASON_NON_NUMERIC)
    low = _num(raw[3], REASON_NON_NUMERIC)
    c = _num(raw[4], REASON_NON_NUMERIC)
    volume = int(_num(raw[5], REASON_NON_NUMERIC)) if len(raw) > 5 else 0
    has_oi = len(raw) > 6 and raw[6] not in (None, "")
    oi = _num(raw[6], REASON_NON_NUMERIC) if has_oi else 0.0

    for price in (o, h, low, c):
        if price < 0:
            raise CandleError(REASON_NEGATIVE_PRICE, f"{o},{h},{low},{c}")
    if volume < 0 or oi < 0:
        raise CandleError(REASON_NEGATIVE_PRICE, f"vol={volume},oi={oi}")
    if h < low:
        raise CandleError(REASON_HIGH_LT_LOW, f"high={h},low={low}")
    if o > h or c > h or o < low or c < low:
        raise CandleError(REASON_OHLC_OUT_OF_RANGE, f"o={o},h={h},l={low},c={c}")

    row: Dict[str, Any] = {
        "timestamp_ist": ts,
        "instrument_key": ref.instrument_key,
        "expired_instrument_key": ref.expired_instrument_key,
        "underlying": str(ref.underlying).upper(),
        "expiry": ref.expiry,
        "strike": float(ref.strike),
        "option_type": option_type,
        "open": o, "high": h, "low": low, "close": c,
        "volume": volume,
        "open_interest": oi,
        "source": source,
        "fetched_at": fetched_at,
        "_has_oi": has_oi,
    }
    row["checksum"] = _row_checksum(row)
    return row


def normalize_candles(
    raw_rows: Sequence[Sequence[Any]],
    ref: OptionContractRef,
    *,
    source: str = "upstox",
    fetched_at: Optional[str] = None,
) -> NormalizeResult:
    """Normalize a batch, collecting per-row errors and collection-level DQ flags."""
    candles: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    flags: Dict[str, int] = {}
    seen: set = set()
    prev_ts: Optional[str] = None

    for idx, raw in enumerate(raw_rows or []):
        try:
            row = normalize_candle(raw, ref, source=source, fetched_at=fetched_at)
        except CandleError as err:
            errors.append({"index": idx, "reason": err.reason, "detail": err.detail, "raw": list(raw) if raw is not None else None})
            continue

        ts = row["timestamp_ist"]
        if ts in seen:
            flags[DQ_DUPLICATE_TS] = flags.get(DQ_DUPLICATE_TS, 0) + 1
        seen.add(ts)
        if prev_ts is not None and ts < prev_ts:
            flags[DQ_NON_MONOTONIC] = flags.get(DQ_NON_MONOTONIC, 0) + 1
        prev_ts = ts
        if row["volume"] == 0:
            flags[DQ_ZERO_VOLUME] = flags.get(DQ_ZERO_VOLUME, 0) + 1
        if not row.pop("_has_oi"):
            flags[DQ_MISSING_OI] = flags.get(DQ_MISSING_OI, 0) + 1

        candles.append(row)

    return NormalizeResult(candles=candles, errors=errors, flags=flags)


def manifest_checksum(candles: Sequence[Dict[str, Any]]) -> str:
    """Deterministic over candle content, independent of input ordering."""
    row_sums = sorted(str(c.get("checksum", "")) for c in candles)
    return hashlib.sha256(",".join(row_sums).encode()).hexdigest()


def build_manifest(
    candles: Sequence[Dict[str, Any]],
    ref: OptionContractRef,
    *,
    source: str,
    from_date: str,
    to_date: str,
    fetched_at: str,
    flags: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Summarize a fetched contract-window: coverage, integrity, quality."""
    timestamps = [c["timestamp_ist"] for c in candles]
    return {
        "source": source,
        "underlying": str(ref.underlying).upper(),
        "expiry": ref.expiry,
        "strike": float(ref.strike),
        "option_type": str(ref.option_type).upper(),
        "instrument_key": ref.instrument_key,
        "expired_instrument_key": ref.expired_instrument_key,
        "from_date": from_date,
        "to_date": to_date,
        "row_count": len(candles),
        "first_timestamp": min(timestamps) if timestamps else None,
        "last_timestamp": max(timestamps) if timestamps else None,
        "checksum": manifest_checksum(candles),
        "data_quality_flags": dict(flags or {}),
        "fetched_at": fetched_at,
    }
