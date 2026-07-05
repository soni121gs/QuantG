"""Underlying INDEX 1-minute store — the piece the intraday backtester needs to
evaluate a signal (the options store only holds option contracts).

Same gzipped-CSV house format as options_minute_store, but an index bar has no
strike/expiry — just OHLCV. Written by the historical importer
(scripts/index_1m_ingest_upstox.py, Upstox v3 active index minutes) and by the
live capture (IMD-04). Read by the intraday OOS orchestration.

    data/index_1m/underlying=NIFTY/year=2025/date=2025-01-09.csv.gz
"""
from __future__ import annotations

import csv
import glob
import gzip
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))

STORE_ROOT = os.environ.get(
    "INDEX_1M_STORE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "index_1m"),
)

_FIELDS = ("timestamp_ist", "underlying", "open", "high", "low", "close", "volume")


def _norm_ts(raw: Any) -> str:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00").replace(" ", "T"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).isoformat()


class IndexMinuteStore:
    def __init__(self, root: str = STORE_ROOT):
        self.root = root

    def _path(self, underlying: str, date: str) -> str:
        return os.path.join(self.root, f"underlying={underlying.upper()}", f"year={str(date)[:4]}", f"date={str(date)[:10]}.csv.gz")

    def write_day(self, underlying: str, date: str, candles: List[Dict[str, Any]]) -> str:
        path = self._path(underlying, date)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = sorted(candles, key=lambda c: c["timestamp_ist"])
        with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
        return path

    def get_minutes(self, underlying: str, date: str) -> List[Dict[str, Any]]:
        path = self._path(underlying, date)
        if not os.path.exists(path):
            return []
        out: List[Dict[str, Any]] = []
        with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                out.append({
                    "timestamp_ist": r["timestamp_ist"], "underlying": r["underlying"],
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": int(float(r["volume"] or 0)),
                    "date": r["timestamp_ist"],  # engine/signal code reads 'date'
                })
        return out

    def normalize_candles(self, underlying: str, raw: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """Group raw broker candles ({date,open,high,low,close,volume}) by IST date."""
        by_day: Dict[str, List[Dict[str, Any]]] = {}
        for c in raw or []:
            ts = _norm_ts(c["date"])
            by_day.setdefault(ts[:10], []).append({
                "timestamp_ist": ts, "underlying": underlying.upper(),
                "open": float(c.get("open") or 0), "high": float(c.get("high") or 0),
                "low": float(c.get("low") or 0), "close": float(c.get("close") or 0),
                "volume": int(float(c.get("volume") or 0)),
            })
        return by_day

    def trading_days(self, underlying: Optional[str] = None) -> List[str]:
        u = f"underlying={underlying.upper()}" if underlying else "underlying=*"
        days = set()
        for path in glob.glob(os.path.join(self.root, u, "year=*", "date=*.csv.gz")):
            base = os.path.basename(path)
            days.add(base[len("date="):-len(".csv.gz")])
        return sorted(days)
