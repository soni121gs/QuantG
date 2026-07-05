"""IMD-04 (live wiring): capture underlying INDEX 1-minute bars from the live feed.

Registers as a read-only tick listener on the V3 feed and rolls index ticks into
1-minute bars via the shared aggregator, flushing them to the IndexMinuteStore at
EOD. This is the forward source of the underlying minutes the intraday backtester
needs. Fully guarded by the feed's listener wrapper — it can never affect trading.

Index-only by design: index tokens carry no strike/expiry so they need no contract
resolution. Forward option-contract capture (token -> OptionContractRef) is a
follow-up; the aggregator + options store already support it once refs are wired.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from core.index_minute_store import IndexMinuteStore
from core.options_minute_capture import MinuteBarAggregator

logger = logging.getLogger("quantg.live_index_capture")

# feed instrument key -> canonical underlying
INDEX_KEYS = {
    "NSE_INDEX|Nifty 50": "NIFTY",
    "NSE_INDEX|Nifty Bank": "BANKNIFTY",
}


class LiveIndexCapture:
    def __init__(self, store: IndexMinuteStore = None, key_map: Dict[str, str] = None):
        self.store = store or IndexMinuteStore()
        self.map = dict(key_map or INDEX_KEYS)
        self.agg = MinuteBarAggregator()
        self._bars: Dict[str, list] = {}
        self._ticks = 0

    def on_tick(self, instrument_key: str, tick: Dict[str, Any]) -> None:
        u = self.map.get(instrument_key)
        if not u:
            return
        ts = tick.get("received_at") or tick.get("timestamp")
        ltp = tick.get("ltp")
        if ts is None or ltp in (None, ""):
            return
        self._ticks += 1
        bar = self.agg.add_tick(instrument_key, ts, float(ltp), cum_volume=tick.get("vtt"))
        if bar is not None:
            self._bars.setdefault(u, []).append(bar)

    def flush_day(self, date: str) -> Dict[str, Any]:
        for bar in self.agg.flush():
            u = self.map.get(bar.instrument_key)
            if u:
                self._bars.setdefault(u, []).append(bar)
        written = 0
        rows = 0
        for u, bars in self._bars.items():
            candles = [{
                "timestamp_ist": b.minute_ts, "underlying": u,
                "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
            } for b in bars]
            if candles:
                self.store.write_day(u, date, candles)
                written += 1
                rows += len(candles)
        self._bars = {}
        logger.info("LiveIndexCapture flushed %s index-days (%s bars) for %s", written, rows, date)
        return {"date": date, "underlyings_written": written, "bars": rows, "ticks_seen": self._ticks}

    def health(self) -> Dict[str, Any]:
        return {"index_tokens": len(self.map), "ticks_seen": self._ticks,
                "buffered_bars": sum(len(v) for v in self._bars.values())}
