"""IMD-04: forward 1-minute option-candle capture from the live feed.

Builds QuantG's OWN legal forward dataset — every market day, from ticks QuantG is
already entitled to — so the intraday backtester (IMD-07) keeps getting fresh
out-of-sample evidence without re-hitting the expired-candle API. Aggregates
tick/LTP updates into 1-minute OHLCV/OI bars and flushes completed contract-days
into the SAME store schema as the imported history (IMD-01/03/05).

Design constraints from the task:
  - subscribe only to contracts strategies could trade (caller supplies the refs),
  - fail-closed on absent feed (record a data-quality gap; NEVER fabricate a bar),
  - read-only w.r.t. trading decisions.

Pure logic + injected store → unit-testable with no broker. Feed wiring (the
upstox_market_data_v3 tick callback → on_tick) is the caller's job; nothing here
touches the trading loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.options_minute_schema import OptionContractRef, build_manifest, normalize_candles
from core.options_minute_store import OptionsMinuteStore, expected_minutes

logger = logging.getLogger("quantg.options_minute_capture")

IST = timezone(timedelta(hours=5, minutes=30))


def _minute_floor(ts: str) -> str:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    dt = dt.astimezone(IST).replace(second=0, microsecond=0)
    return dt.isoformat()


@dataclass
class _OpenBar:
    minute_ts: str
    open: float
    high: float
    low: float
    close: float
    start_cum: float
    last_cum: float
    oi: float


@dataclass
class MinuteBar:
    instrument_key: str
    minute_ts: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: float

    def as_raw(self) -> List[Any]:
        return [self.minute_ts, self.open, self.high, self.low, self.close, self.volume, self.oi]


class MinuteBarAggregator:
    """Rolls tick/LTP updates into completed 1-minute bars, per instrument."""

    def __init__(self):
        self._open: Dict[str, _OpenBar] = {}
        self._prev_cum: Dict[str, float] = {}

    def add_tick(
        self, instrument_key: str, timestamp: str, ltp: float,
        cum_volume: Optional[float] = None, oi: Optional[float] = None,
    ) -> Optional[MinuteBar]:
        """Feed one tick. Returns the PREVIOUS minute's bar when the minute rolls."""
        minute = _minute_floor(timestamp)
        price = float(ltp)
        cum = float(cum_volume) if cum_volume is not None else self._prev_cum.get(instrument_key, 0.0)
        oi_v = float(oi) if oi is not None else (self._open[instrument_key].oi if instrument_key in self._open else 0.0)

        completed: Optional[MinuteBar] = None
        cur = self._open.get(instrument_key)
        if cur is None or minute > cur.minute_ts:
            if cur is not None and minute > cur.minute_ts:
                completed = self._close(instrument_key, cur)
            self._open[instrument_key] = _OpenBar(
                minute_ts=minute, open=price, high=price, low=price, close=price,
                start_cum=self._prev_cum.get(instrument_key, 0.0), last_cum=cum, oi=oi_v,
            )
        else:
            cur.high = max(cur.high, price)
            cur.low = min(cur.low, price)
            cur.close = price
            cur.last_cum = cum
            cur.oi = oi_v
        self._prev_cum[instrument_key] = cum
        return completed

    def _close(self, instrument_key: str, bar: _OpenBar) -> MinuteBar:
        vol = int(max(0.0, bar.last_cum - bar.start_cum))
        return MinuteBar(instrument_key, bar.minute_ts, bar.open, bar.high, bar.low, bar.close, vol, bar.oi)

    def flush(self, instrument_key: Optional[str] = None) -> List[MinuteBar]:
        """Close open bars (call at session end). Empties the buffer of closed keys."""
        keys = [instrument_key] if instrument_key else list(self._open.keys())
        out = []
        for k in keys:
            bar = self._open.pop(k, None)
            if bar is not None:
                out.append(self._close(k, bar))
        return out


class OptionsMinuteCapture:
    """Coordinates aggregation → buffered contract-days → store, with health counters."""

    def __init__(
        self,
        store: OptionsMinuteStore,
        contract_refs: Dict[str, OptionContractRef],
        *,
        source: str = "upstox",
    ):
        self.store = store
        self.refs = dict(contract_refs)
        self.source = source
        self.agg = MinuteBarAggregator()
        self._bars: Dict[str, List[MinuteBar]] = {}  # instrument_key -> completed bars today
        self._bars_written = 0
        self._ticks = 0
        self._last_tick_at: Optional[datetime] = None

    def on_tick(self, instrument_key: str, timestamp: str, ltp: float,
                cum_volume: Optional[float] = None, oi: Optional[float] = None) -> None:
        if instrument_key not in self.refs:
            return  # only capture subscribed, tradeable contracts
        self._ticks += 1
        self._last_tick_at = datetime.now(timezone.utc)
        completed = self.agg.add_tick(instrument_key, timestamp, ltp, cum_volume, oi)
        if completed is not None:
            self._bars.setdefault(instrument_key, []).append(completed)

    def flush_day(self, date: str, *, fetched_at: Optional[str] = None) -> Dict[str, Any]:
        """Close open bars and persist each buffered contract-day. Fail-closed."""
        for bar in self.agg.flush():
            self._bars.setdefault(bar.instrument_key, []).append(bar)
        fetched_at = fetched_at or datetime.now(timezone.utc).astimezone().isoformat()
        written, gaps = 0, {}
        for key, bars in self._bars.items():
            ref = self.refs.get(key)
            if ref is None or not bars:
                continue
            res = normalize_candles([b.as_raw() for b in bars], ref, source=self.source, fetched_at=fetched_at)
            if not res.candles:
                gaps[key] = "NO_VALID_BARS"
                continue
            manifest = build_manifest(res.candles, ref, source=self.source, from_date=date,
                                      to_date=date, fetched_at=fetched_at, flags=res.flags)
            self.store.write_contract_day(res.candles, manifest)
            written += 1
            self._bars_written += len(res.candles)
            missing = len(expected_minutes(date)) - len(res.candles)
            if missing > 0:
                gaps[key] = f"MISSING_{missing}_MINUTES"
        self._bars = {}
        return {"date": date, "contracts_written": written, "bars_written": self._bars_written, "data_quality_gaps": gaps}

    def health(self) -> Dict[str, Any]:
        stale = None
        if self._last_tick_at is not None:
            stale = (datetime.now(timezone.utc) - self._last_tick_at).total_seconds()
        return {
            "subscribed_contracts": len(self.refs),
            "ticks_seen": self._ticks,
            "bars_written": self._bars_written,
            "buffered_bars": sum(len(v) for v in self._bars.values()),
            "stale_feed_seconds": stale,
        }
