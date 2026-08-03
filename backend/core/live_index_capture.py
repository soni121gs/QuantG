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

import session_times
from core.index_minute_store import IndexMinuteStore
from core.options_minute_capture import MinuteBarAggregator, _minute_floor

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

# Which session window each index's bars belong to. SENSEX is BSE, which
# `session_times` deliberately keeps at the pre-change 15:30 close.
INDEX_SEGMENTS = {"NIFTY": "NSE_FO", "BANKNIFTY": "NSE_FO", "SENSEX": "BSE_FO"}


class LiveIndexCapture:
    def __init__(self, store: IndexMinuteStore = None, key_map: Dict[str, str] = None):
        self.store = store or IndexMinuteStore()
        self.map = dict(key_map or INDEX_KEYS)
        self.agg = MinuteBarAggregator()
        self._bars: Dict[str, list] = {}
        self._ticks = 0
        self._rejected_price = 0
        self._rejected_session = 0

    def on_tick(self, instrument_key: str, tick: Dict[str, Any]) -> None:
        u = self.map.get(instrument_key)
        if not u:
            return
        ts = tick.get("received_at") or tick.get("timestamp")
        ltp = tick.get("ltp")
        if ts is None or ltp in (None, ""):
            return

        # A non-positive print is not a price. `ltp in (None, "")` lets 0.0
        # through, and ONE 0.0 tick sets the day's low to zero, so rng_pct blows
        # up to ~100% and `regime_classifier` returns HIGH_VOL_CHOP at confidence
        # 1.0 — which the RAE router reads as an explicit stand-down. Measured
        # 2026-08-03 on SENSEX (BSE prints 0 pre-open, before it starts computing
        # the index): every seller in the book stood down on a quiet range day.
        try:
            price = float(ltp)
        except (TypeError, ValueError):
            return
        if price <= 0:
            self._rejected_price += 1
            return

        # Only aggregate ticks INSIDE the trading session. The feed connects
        # whenever the token is refreshed — 08:32 IST on 2026-08-03, 43 minutes
        # before the open — and pre-open prints carry the PREVIOUS close. Those
        # became bars[0], so the classifier's `ret_pct` (which anchors on
        # bars[0].open) turned gap-inclusive: a flat +0.13% NIFTY session read
        # TREND_UP at confidence 0.90 and the sellers lost their own regime.
        # The EOD store never showed this because on earlier days the feed
        # happened to connect after 09:15.
        try:
            minute_ts = _minute_floor(ts)
            hh, mm = int(minute_ts[11:13]), int(minute_ts[14:16])
        except (ValueError, IndexError, TypeError):
            return
        if not session_times.in_session(hh, mm, INDEX_SEGMENTS.get(u, "NSE_FO")):
            self._rejected_session += 1
            return

        self._ticks += 1
        bar = self.agg.add_tick(instrument_key, ts, price, cum_volume=tick.get("vtt"))
        if bar is not None:
            self._bars.setdefault(u, []).append(bar)

    def flush_day(self, date: str) -> Dict[str, Any]:
        for bar in self.agg.flush():
            u = self.map.get(bar.instrument_key)
            if u:
                self._bars.setdefault(u, []).append(bar)
        written = 0
        rows = 0
        failed: Dict[str, str] = {}
        for u, bars in self._bars.items():
            candles = [{
                "timestamp_ist": b.minute_ts, "underlying": u,
                "open": b.open, "high": b.high, "low": b.low, "close": b.close, "volume": b.volume,
            } for b in bars]
            if not candles:
                continue
            # Per-underlying isolation: one store failure (e.g. a root-owned ./data
            # giving the container user EPERM) must not abort the other underlyings
            # OR leave the buffer unflushed — stale bars carried into the next
            # session would mix two days and corrupt the regime classifier.
            try:
                self.store.write_day(u, date, candles)
                written += 1
                rows += len(candles)
            except Exception as exc:  # noqa: BLE001
                failed[u] = f"{type(exc).__name__}: {exc}"
                logger.error("LiveIndexCapture write failed %s %s: %s", u, date, exc, exc_info=True)
        self._bars = {}
        logger.info("LiveIndexCapture flushed %s index-days (%s bars) for %s (failed=%s)",
                    written, rows, date, len(failed))
        return {"date": date, "underlyings_written": written, "bars": rows,
                "ticks_seen": self._ticks, "failed": failed}

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
                # Must honour `wanted` exactly as add_row does. Without this filter a
                # request for NIFTY also returned BANKNIFTY's and SENSEX's open bars,
                # so the regime classifier saw 3 "bars" at 23700/56400/75900 and read
                # ret_pct=220% at maturity 3/45 -> RANGE confidence 0.027 all session.
                if not u or (wanted and u != wanted):
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
                "buffered_bars": sum(len(v) for v in self._bars.values()),
                "rejected_nonpositive_price": self._rejected_price,
                "rejected_outside_session": self._rejected_session}
