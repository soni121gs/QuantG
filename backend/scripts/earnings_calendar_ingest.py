#!/usr/bin/env python3
"""Ingest earnings dates from a CSV into data/earnings_dates/.

Expected columns: symbol,date. Optional: event_type,source,headline,company_name.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.earnings_calendar import store_events, events_for  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--verify-symbol", default="")
    ap.add_argument("--verify-start", default="")
    ap.add_argument("--verify-end", default="")
    args = ap.parse_args()
    with open(args.csv_path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    written = store_events(rows)
    print(f"stored_events: {written}")
    if args.verify_symbol and args.verify_start:
        events = events_for(args.verify_symbol, args.verify_start, args.verify_end or args.verify_start)
        print(f"verify {args.verify_symbol}: {len(events)} event(s)")
        for event in events[:10]:
            print(event)


if __name__ == "__main__":
    main()
