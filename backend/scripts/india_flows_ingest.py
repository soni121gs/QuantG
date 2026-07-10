#!/usr/bin/env python3
"""IA-4: fetch + store NSE FII/DII daily net cash flows (forward capture).

Run post-market daily (host cron), MUST be -u root because the ./data bind mount
is root-owned (same as bhavcopy_ingest):
    docker exec -u root quantg-backend python /app/scripts/india_flows_ingest.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import india_flows as f


def main():
    rows = f.fetch_fiidii()
    n = f.store_rows(rows)
    print(f"fetched {len(rows)} FII/DII row(s), stored {n}. total days: {len(f.available_days())}")
    for r in rows:
        print(f"  {r.get('date')}: FII net ₹{r.get('fii_net', 0):,.0f}cr | DII net ₹{r.get('dii_net', 0):,.0f}cr")


if __name__ == "__main__":
    main()
