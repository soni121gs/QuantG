#!/usr/bin/env python3
"""
NSE F&O Bhavcopy (UDiFF) ingest — the free EOD options-history data pipeline.

Solves the "data wall": Upstox 404s on expired-option history, so the option
strategies could never be backtested. NSE publishes a free daily F&O bhavcopy
(UDiFF format) containing per-contract EOD OHLC, settlement price, underlying
price, OI and volume for EVERY index/stock derivative. That is enough to
backtest the slow, held-to-theta strategies (credit spreads, 2h+ holds) — which
attribution shows are the only profitable cluster.

- Source: https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip
- Filter: index options (FinInstrmTp=IDO) for the underlyings the book trades.
- Store: one gzipped CSV per trading day under data/bhavcopy_fo/<YYYY>/.
- Idempotent: skips days already stored; skips holidays/weekends (404).
- Stdlib only (urllib/zipfile/csv/gzip) — no pandas/pyarrow needed to backfill.

Usage:
    python3 bhavcopy_ingest.py 2024-01-01 2025-12-31
    python3 bhavcopy_ingest.py 2025-06-01 2025-06-30 --underlyings NIFTY,BANKNIFTY
"""
import sys
import os
import csv
import gzip
import io
import zipfile
import time
import argparse
import urllib.request
import urllib.error
from datetime import date, timedelta

# Index underlyings the live book actually trades (BSE SENSEX/BANKEX are a
# separate BSE bhavcopy source — not in the NSE file).
DEFAULT_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}

STORE_ROOT = os.environ.get(
    "BHAVCOPY_STORE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "bhavcopy_fo"),
)

URL_TMPL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Columns we keep (clean, backtester-friendly subset of the 34-col UDiFF file).
OUT_COLS = [
    "date", "underlying", "expiry", "strike", "option_type", "symbol",
    "open", "high", "low", "close", "settle", "underlying_price",
    "oi", "chg_in_oi", "volume", "lot_size",
]


def out_path(d: date) -> str:
    return os.path.join(STORE_ROOT, str(d.year), f"BhavCopy_FO_{d.strftime('%Y%m%d')}.csv.gz")


def download(d: date, retries: int = 3) -> bytes | None:
    """Return the raw zip bytes, or None on 404 (non-trading day)."""
    url = URL_TMPL.format(d=d.strftime("%Y%m%d"))
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None  # holiday / weekend
            time.sleep(2 * (attempt + 1))
        except Exception:
            time.sleep(2 * (attempt + 1))
    print(f"  !! failed after {retries} retries: {d}")
    return None


def parse_and_filter(zip_bytes: bytes, underlyings: set[str]) -> list[dict]:
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    raw = z.read(z.namelist()[0]).decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(raw))
    out = []
    for row in reader:
        if row.get("FinInstrmTp") != "IDO":  # index options only
            continue
        sym = row.get("TckrSymb", "")
        if sym not in underlyings:
            continue
        out.append({
            "date": row["TradDt"],
            "underlying": sym,
            "expiry": row["XpryDt"],
            "strike": row["StrkPric"],
            "option_type": row["OptnTp"],
            "symbol": row["FinInstrmNm"],
            "open": row["OpnPric"],
            "high": row["HghPric"],
            "low": row["LwPric"],
            "close": row["ClsPric"],
            "settle": row["SttlmPric"],
            "underlying_price": row["UndrlygPric"],
            "oi": row["OpnIntrst"],
            "chg_in_oi": row["ChngInOpnIntrst"],
            "volume": row["TtlTradgVol"],
            "lot_size": row["NewBrdLotQty"],
        })
    return out


def write_day(d: date, rows: list[dict]) -> None:
    path = out_path(d)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(rows)


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", help="YYYY-MM-DD")
    ap.add_argument("end", help="YYYY-MM-DD")
    ap.add_argument("--underlyings", default=",".join(sorted(DEFAULT_UNDERLYINGS)))
    ap.add_argument("--overwrite", action="store_true", help="re-download days already stored")
    args = ap.parse_args()

    underlyings = {u.strip().upper() for u in args.underlyings.split(",") if u.strip()}
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    print(f"store: {STORE_ROOT}")
    print(f"underlyings: {sorted(underlyings)}")
    print(f"range: {start} -> {end}\n")

    days = trading = holidays = skipped = total_rows = 0
    for d in daterange(start, end):
        days += 1
        if d.weekday() >= 5:  # Sat/Sun — skip without hitting NSE
            continue
        if os.path.exists(out_path(d)) and not args.overwrite:
            skipped += 1
            continue
        blob = download(d)
        if blob is None:
            holidays += 1
            continue
        rows = parse_and_filter(blob, underlyings)
        if not rows:
            holidays += 1
            continue
        write_day(d, rows)
        trading += 1
        total_rows += len(rows)
        print(f"  {d}  rows={len(rows):>5}  -> {os.path.relpath(out_path(d), STORE_ROOT)}")
        time.sleep(0.4)  # be polite to NSE

    print(f"\nDONE. days_scanned={days} written={trading} already_had={skipped} "
          f"non_trading={holidays} total_rows={total_rows}")


if __name__ == "__main__":
    main()
