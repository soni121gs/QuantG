#!/usr/bin/env python3
"""Ingest NSE participant-wise F&O OI CSV files into data/participant_oi/.

Use with manually downloaded/public archive CSV files:
    python scripts/participant_oi_ingest.py 2026-07-17 path/to/file.csv
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.india_flows import parse_participant_oi_csv, store_participant_oi, participant_oi_days  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("date", help="YYYY-MM-DD trading date")
    ap.add_argument("csv_path")
    args = ap.parse_args()
    with open(args.csv_path, "r", encoding="utf-8-sig") as fh:
        rows = parse_participant_oi_csv(fh.read())
    stored = store_participant_oi(args.date, rows)
    print(f"stored_participant_oi_rows: {stored}")
    print(f"total_days: {len(participant_oi_days())}")
    for row in rows:
        print(row)


if __name__ == "__main__":
    main()
