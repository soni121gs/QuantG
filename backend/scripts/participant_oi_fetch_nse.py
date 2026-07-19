#!/usr/bin/env python3
"""Fetch NSE participant-wise F&O open-interest reports into data/participant_oi/.

The official NSE all-reports page marks this report discontinued from
2024-07-08, so historical backfill stops at the last available trading day.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.india_flows import parse_participant_oi_csv, participant_oi_days, store_participant_oi  # noqa: E402

NSE_REPORT_URL = "https://www.nseindia.com/api/reports"
NSE_HOME = "https://www.nseindia.com/"
REFERER = "https://www.nseindia.com/all-reports-derivatives"
REPORT_NAME = "F&O - Participant wise Open Interest(csv)"
DISCONTINUED_ON = date(2024, 7, 8)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _parse_date(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _nse_date(d: date) -> str:
    return d.strftime("%d-%b-%Y")


def _daterange(start: date, end: date) -> Iterable[date]:
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _opener() -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    opener.addheaders = [
        ("User-Agent", UA),
        ("Accept", "text/csv,application/json,*/*"),
        ("Accept-Language", "en-US,en;q=0.9"),
        ("Referer", REFERER),
    ]
    try:
        opener.open(NSE_HOME, timeout=20).read()
    except Exception:
        pass
    return opener


def fetch_report(d: date, opener: Optional[urllib.request.OpenerDirector] = None) -> str:
    archives = [{
        "name": REPORT_NAME,
        "type": "archives",
        "category": "derivatives",
        "section": "equity",
    }]
    params = {
        "archives": json.dumps(archives, separators=(",", ":")),
        "date": _nse_date(d),
        "type": "equity",
        "mode": "single",
    }
    url = f"{NSE_REPORT_URL}?{urllib.parse.urlencode(params)}"
    opener = opener or _opener()
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/csv,application/json,*/*",
        "Referer": REFERER,
        "Accept-Language": "en-US,en;q=0.9",
    })
    with opener.open(req, timeout=30) as resp:
        return resp.read().decode("utf-8-sig", "replace")


def ingest_range(start: date, end: date, *, overwrite: bool = False, sleep_sec: float = 0.2) -> dict:
    end = min(end, DISCONTINUED_ON - timedelta(days=1))
    opener = _opener()
    existing = set(participant_oi_days())
    summary = {"scanned": 0, "stored_days": 0, "rows": 0, "skipped_existing": 0, "unavailable": 0, "errors": 0}
    for d in _daterange(start, end):
        if d.weekday() >= 5:
            continue
        ds = d.isoformat()
        summary["scanned"] += 1
        if not overwrite and ds in existing:
            summary["skipped_existing"] += 1
            continue
        try:
            raw = fetch_report(d, opener)
            if raw.lstrip().startswith("{"):
                summary["unavailable"] += 1
                continue
            rows = parse_participant_oi_csv(raw)
            if not rows:
                summary["unavailable"] += 1
                continue
            summary["rows"] += store_participant_oi(ds, rows, source="nse_all_reports")
            summary["stored_days"] += 1
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                summary["unavailable"] += 1
            else:
                summary["errors"] += 1
        except Exception:
            summary["errors"] += 1
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    summary["total_days"] = len(participant_oi_days())
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch NSE participant-wise F&O OI archive reports")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--sleep-sec", type=float, default=0.2)
    args = ap.parse_args()
    summary = ingest_range(
        _parse_date(args.start), _parse_date(args.end),
        overwrite=args.overwrite, sleep_sec=max(0.0, args.sleep_sec),
    )
    print(summary)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
