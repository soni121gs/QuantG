#!/usr/bin/env python3
"""Fetch NSE board-meeting result dates into data/earnings_dates/.

NSE's board-meetings feed is the least ambiguous public source for an event date:
the meeting date is when the board considers/approves financial results. The
event-conditional judge can later decide whether to use T-1, T, or T+1.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.earnings_calendar import events_for, store_events  # noqa: E402


DEFAULT_TOP30_FNO = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "LT", "SBIN",
    "BHARTIARTL", "AXISBANK", "KOTAKBANK", "ITC", "HINDUNILVR",
    "BAJFINANCE", "M&M", "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO",
    "ASIANPAINT", "NTPC", "POWERGRID", "TATASTEEL", "ADANIENT",
    "ADANIPORTS", "ONGC", "COALINDIA", "WIPRO", "HCLTECH", "TECHM",
    "BAJAJFINSV",
]

NSE_HOME = "https://www.nseindia.com/"
NSE_BOARD_URL = "https://www.nseindia.com/api/corporate-board-meetings"
REFERER = "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
RESULT_KEYWORDS = ("financial result", "financial results", "audited result", "unaudited result")


def _parse_date(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _nse_date(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _chunks(start: date, end: date, days: int) -> Iterable[tuple[date, date]]:
    cur = start
    while cur <= end:
        nxt = min(end, cur + timedelta(days=days - 1))
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def _request(session: urllib.request.OpenerDirector, url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/csv,application/json,text/plain,*/*",
            "Referer": REFERER,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with session.open(req, timeout=30) as resp:
        return resp.read()


def _session() -> urllib.request.OpenerDirector:
    cookies = urllib.request.HTTPCookieProcessor()
    opener = urllib.request.build_opener(cookies)
    try:
        _request(opener, NSE_HOME)
    except Exception:
        pass
    return opener


def fetch_board_meetings(symbol: str, start: date, end: date, *, retries: int = 3) -> List[Dict[str, Any]]:
    params = {
        "index": "equities",
        "from_date": _nse_date(start),
        "to_date": _nse_date(end),
        "symbol": symbol,
        "csv": "true",
    }
    url = f"{NSE_BOARD_URL}?{urllib.parse.urlencode(params)}"
    opener = _session()
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            raw = _request(opener, url).decode("utf-8-sig", "replace").strip()
            if not raw:
                return []
            if raw[:40].lower().startswith("<!doctype html") or raw[:20].lower().startswith("<html"):
                raise RuntimeError("NSE returned HTML shell instead of board-meetings data")
            if raw[0] in "[{":
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    data = payload.get("data") or payload.get("rows") or payload.get("items") or []
                    return data if isinstance(data, list) else []
                return payload if isinstance(payload, list) else []
            return [_clean_row(row) for row in csv.DictReader(io.StringIO(raw))]
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"NSE board-meetings fetch failed for {symbol} {start}:{end}: {last_error}")


def _is_results_row(row: Dict[str, Any]) -> bool:
    text = " ".join(str(row.get(k) or "") for k in ("Purpose", "PURPOSE", "Details", "DETAILS")).lower()
    return any(k in text for k in RESULT_KEYWORDS)


def _clean_key(key: Any) -> str:
    return " ".join(str(key or "").strip().upper().split())


def _clean_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {_clean_key(k): v for k, v in row.items()}


def normalize_rows(rows: Iterable[Dict[str, Any]], source: str = "nse_board_meetings") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        if not _is_results_row(row):
            continue
        purpose = row.get("Purpose") or row.get("PURPOSE") or "Financial Results"
        details = row.get("Details") or row.get("DETAILS") or ""
        out.append({
            "symbol": row.get("SYMBOL") or row.get("symbol"),
            "date": row.get("MEETING DATE") or row.get("meeting_date"),
            "event_type": "earnings",
            "source": source,
            "headline": f"{purpose}: {details}".strip(": "),
        })
    return out


def collect_events(symbols: List[str], start: date, end: date, *, chunk_days: int, sleep_sec: float) -> Dict[str, Any]:
    all_events: List[Dict[str, Any]] = []
    errors: List[str] = []
    for symbol in symbols:
        for a, b in _chunks(start, end, chunk_days):
            try:
                all_events.extend(normalize_rows(fetch_board_meetings(symbol, a, b)))
            except Exception as exc:
                errors.append(str(exc))
            if sleep_sec > 0:
                time.sleep(sleep_sec)
    written = store_events(all_events)
    return {"symbols": len(symbols), "events": len(all_events), "stored_rows": written, "errors": errors[:20]}


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch NSE financial-results board-meeting dates")
    ap.add_argument("--from", dest="start", default="")
    ap.add_argument("--to", dest="end", default="")
    ap.add_argument("--forward-days", type=int, default=0,
                    help="If set, fetch from today-N through today+N for weekly forward refresh")
    ap.add_argument("--symbols", default=",".join(DEFAULT_TOP30_FNO))
    ap.add_argument("--chunk-days", type=int, default=365)
    ap.add_argument("--sleep-sec", type=float, default=0.25)
    ap.add_argument("--verify-symbol", default="RELIANCE")
    ap.add_argument("--debug-rows", action="store_true")
    args = ap.parse_args()

    today = date.today()
    if args.forward_days > 0:
        start = today - timedelta(days=args.forward_days)
        end = today + timedelta(days=args.forward_days)
    else:
        if not args.start or not args.end:
            ap.error("--from and --to are required unless --forward-days is set")
        start = _parse_date(args.start)
        end = _parse_date(args.end)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    result = collect_events(symbols, start, end, chunk_days=max(1, args.chunk_days), sleep_sec=max(0.0, args.sleep_sec))
    print(result)
    if args.debug_rows and symbols:
        sample = fetch_board_meetings(symbols[0], start, min(end, start + timedelta(days=max(1, args.chunk_days - 1))))
        print(f"debug {symbols[0]} rows: {len(sample)}")
        for row in sample[:5]:
            print(row)
    if args.verify_symbol:
        events = events_for(args.verify_symbol, start.isoformat(), end.isoformat())
        print(f"verify {args.verify_symbol}: {len(events)} event(s)")
        for event in events[:12]:
            print(event)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
