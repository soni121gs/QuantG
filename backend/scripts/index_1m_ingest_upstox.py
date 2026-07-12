"""Historical underlying INDEX 1-minute importer (Upstox v3 active historical).

Fetches index minute candles (NIFTY/BANKNIFTY) for a date window and stores them
per day (core/index_minute_store). This is the underlying series the intraday
backtester evaluates signals on — the companion to the options importer (IMD-03).

Upstox v3 limits 1-minute history to ~1 month per request, so we chunk by ~25 days.

    python scripts/index_1m_ingest_upstox.py --from 2025-01-01 --to 2025-01-31 --underlyings NIFTY
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.index_minute_store import IndexMinuteStore

logger = logging.getLogger("quantg.index_1m_ingest")

INDEX_KEY = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    # BSE index key (Upstox v3 historical supports BSE indices) — added 2026-07-13 so
    # the regime judge can grade the SENSEX specialists instead of ERRing on no data.
    "SENSEX": "BSE_INDEX|SENSEX",
}
DEFAULT_UNDERLYINGS = ["NIFTY", "BANKNIFTY"]


def _chunks(start: str, end: str, days: int = 25):
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    cur = s
    while cur <= e:
        chunk_end = min(cur + timedelta(days=days - 1), e)
        yield cur.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d")
        cur = chunk_end + timedelta(days=1)


def ingest(gateway, store: IndexMinuteStore, underlyings: List[str], start: str, end: str) -> dict:
    summary = {"underlyings": {}, "days_written": 0, "rows": 0}
    for u in underlyings:
        u = u.upper()
        key = INDEX_KEY.get(u)
        if not key:
            continue
        days_written = 0
        for c_start, c_end in _chunks(start, end):
            raw = gateway.get_historical_candles_v3(key, unit="minutes", interval=1, from_date=c_start, to_date=c_end)
            by_day = store.normalize_candles(u, raw or [])
            for date, candles in by_day.items():
                store.write_day(u, date, candles)
                days_written += 1
                summary["rows"] += len(candles)
        summary["underlyings"][u] = days_written
        summary["days_written"] += days_written
    return summary


def _gateway_from_broker_keys():  # pragma: no cover
    from pymongo import MongoClient
    from brokers.upstox_gateway import UpstoxGateway
    import server
    db = MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017")).quantg
    bk = db.broker_keys.find_one({"broker": "upstox"}) or {}
    token = server.decrypt_secret(bk.get("access_token"))
    if not token:
        raise RuntimeError("No usable Upstox access token — reconnect first.")
    return UpstoxGateway(access_token=token)


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Historical index 1-minute importer")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--underlyings", default=",".join(DEFAULT_UNDERLYINGS))
    args = ap.parse_args(argv)
    underlyings = [u.strip().upper() for u in args.underlyings.split(",") if u.strip()]
    gateway = _gateway_from_broker_keys()
    summary = ingest(gateway, IndexMinuteStore(), underlyings, args.start, args.end)
    print(f"Index ingest: {summary}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
