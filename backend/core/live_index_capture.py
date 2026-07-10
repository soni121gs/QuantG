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
from typing import Any, Dict, List

from core.index_minute_store import IndexMinuteStore
from core.options_minute_capture import MinuteBarAggregator

logger = logging.getLogger("quantg.live_index_capture")

# feed instrument key -> canonical underlying. SENSEX (BSE) is already subscribed
# as a baseline token, so mapping it here starts aggregating its 1-minute bars too
# — used by the RAE-1 fine-regime classifier for SENSEX (QG-O4) and stored for
# future SENSEX studies. Percentage-based classifier thresholds transfer across
# indices, so no per-index tuning is needed.
INDEX_KEYS = {
    "NSE_INDEX|Nifty 50": "NIFTY",
    "NSE_INDEX|Nifty Bank": "BANKNIFTY",
    "BSE_INDEX|SENSEX": "SENSEX",
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

    def snapshot_minutes(self, underlying: str = None, include_open: bool = True) -> List[Dict[str, Any]]:
        """Return buffered live bars without mutating the EOD flush buffer."""
        wanted = str(underlying or "").upper()
        rows: List[Dict[str, Any]] = []

        def add_row(u: str, b) -> None:
            if wanted and u != wanted:
                return
            rows.append({
                "timestamp_ist": b.minute_ts,
                "underlying": u,
                "date": b.minute_ts,
                "open": float(b.open),
                "high": float(b.high),
                "low": float(b.low),
                "close": float(b.close),
                "volume": int(getattr(b, "volume", 0) or 0),
            })

        for u, bars in self._bars.items():
            for bar in bars:
                add_row(u, bar)

        if include_open:
            for key, bar in self.agg._open.items():
                u = self.map.get(key)
                if not u:
                    continue
                rows.append({
                    "timestamp_ist": bar.minute_ts,
                    "underlying": u,
                    "date": bar.minute_ts,
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(max(0.0, float(bar.last_cum or 0) - float(bar.start_cum or 0))),
                })

        return sorted(rows, key=lambda r: str(r.get("timestamp_ist") or r.get("date") or ""))

    def health(self) -> Dict[str, Any]:
        return {"index_tokens": len(self.map), "ticks_seen": self._ticks,
                "buffered_bars": sum(len(v) for v in self._bars.values())}
