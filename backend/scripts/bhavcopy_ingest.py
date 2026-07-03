#!/usr/bin/env python3
"""
F&O Bhavcopy (UDiFF) ingest — the free EOD options-history data pipeline.

Solves the "data wall": Upstox 404s on expired-option history, so the option
strategies could never be backtested. NSE (and BSE) publish a free daily F&O
bhavcopy (UDiFF format) with per-contract EOD OHLC, settlement price, underlying
price, OI and volume for EVERY index derivative. That is enough to backtest the
slow, held-to-theta strategies (credit spreads, 2h+ holds) — the only profitable
cluster per attribution.

Stores per trading day, gzipped CSV, both:
  - index OPTIONS (FinInstrmTp=IDO)  -> the tradable legs (strike/type/settle/oi)
  - index FUTURES (FinInstrmTp=IDF)  -> real daily OHLC of the underlying, so the
                                        backtester generates signals on true bars
                                        instead of a flat close-only series.

Sources:
  - NSE: https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<d>_F_0000.csv.zip  (works via curl/urllib)
  - BSE: https://www.bseindia.com/download/BhavCopy/Derivative/BhavCopy_BSE_FO_0_0_0_<d>_F_0000.CSV.zip
         (Akamai-gated: a bare HTTP client gets the SPA shell, not the zip. The
          code validates the ZIP magic and skips+warns rather than storing HTML.
          Populate BSE via a browser session or manual drop until a headless
          fetcher is added — see fetch_bse() note. SENSEX/BANKEX live here.)

Stdlib only (urllib/zipfile/csv/gzip) — no pandas/pyarrow needed.

Usage:
    python3 bhavcopy_ingest.py 2024-01-01 2025-12-31                 # NSE (default)
    python3 bhavcopy_ingest.py 2025-06-01 2025-06-30 --source bse    # BSE (SENSEX/BANKEX)
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

# --- source registry ----------------------------------------------------------
SOURCES = {
    "nse": {
        "url": "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip",
        "underlyings": {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"},
        "prefix": "BhavCopy_FO_",           # keep legacy NSE filename (494 files already stored)
        "referer": "https://www.nseindia.com/",
    },
    "bse": {
        "url": "https://www.bseindia.com/download/BhavCopy/Derivative/BhavCopy_BSE_FO_0_0_0_{d}_F_0000.CSV.zip",
        "underlyings": {"SENSEX", "BANKEX", "SENSEX50"},
        "prefix": "BhavCopy_BSE_FO_",
        "referer": "https://www.bseindia.com/markets/Derivatives/DeriReports.aspx",
    },
}

STORE_ROOT = os.environ.get(
    "BHAVCOPY_STORE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "bhavcopy_fo"),
)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Clean, backtester-friendly subset of the 34-col UDiFF file. instr_type keeps
# IDO (option) vs IDF (future) apart; futures rows have blank strike/option_type.
OUT_COLS = [
    "date", "instr_type", "underlying", "expiry", "strike", "option_type", "symbol",
    "open", "high", "low", "close", "settle", "underlying_price",
    "oi", "chg_in_oi", "volume", "lot_size",
]


def out_path(d: date, prefix: str) -> str:
    return os.path.join(STORE_ROOT, str(d.year), f"{prefix}{d.strftime('%Y%m%d')}.csv.gz")


def download(d: date, src: dict, retries: int = 3) -> bytes | None:
    """Return raw zip bytes, or None on 404 / non-zip (holiday or gated host)."""
    url = src["url"].format(d=d.strftime("%Y%m%d"))
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/zip,*/*",
                "Referer": src["referer"],
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                blob = resp.read()
            if blob[:2] != b"PK":  # not a zip — gated SPA shell or error page
                if attempt == 0:
                    print(f"  ~~ {d}: non-zip response ({len(blob)}B) — host gated/holiday")
                return None
            return blob
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
        itp = row.get("FinInstrmTp")
        if itp not in ("IDO", "IDF"):  # index options + index futures only
            continue
        sym = row.get("TckrSymb", "")
        if sym not in underlyings:
            continue
        out.append({
            "date": row["TradDt"],
            "instr_type": itp,
            "underlying": sym,
            "expiry": row["XpryDt"],
            "strike": row.get("StrkPric", ""),
            "option_type": row.get("OptnTp", ""),
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


def write_day(d: date, prefix: str, rows: list[dict]) -> None:
    path = out_path(d, prefix)
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


def _ingest_local_zips(zip_dir: str, prefix: str, underlyings: set[str], overwrite: bool) -> None:
    """Parse pre-downloaded bhavcopy .zip files (any source) into the gz store.
    Filenames must contain the 8-digit trading date (YYYYMMDD). Robust to
    non-zip files (skips them). Use this for BSE SENSEX/BANKEX, which the
    automated downloader cannot reach (Akamai bot-gate)."""
    import glob as _glob
    import re as _re
    files = sorted(_glob.glob(os.path.join(zip_dir, "*.zip")) + _glob.glob(os.path.join(zip_dir, "*.ZIP")))
    print(f"ingest-from-zips: {len(files)} zip(s) in {zip_dir}")
    print(f"underlyings: {sorted(underlyings)}  store: {STORE_ROOT}\n")
    written = skipped = bad = total_rows = 0
    for path in files:
        m = _re.search(r"(\d{8})", os.path.basename(path))
        if not m:
            print(f"  ?? no date in filename, skipped: {os.path.basename(path)}")
            continue
        d = date(int(m.group(1)[:4]), int(m.group(1)[4:6]), int(m.group(1)[6:8]))
        if os.path.exists(out_path(d, prefix)) and not overwrite:
            skipped += 1
            continue
        try:
            with open(path, "rb") as f:
                blob = f.read()
            if blob[:2] != b"PK":
                bad += 1
                print(f"  !! not a zip (Akamai HTML?), skipped: {os.path.basename(path)}")
                continue
            rows = parse_and_filter(blob, underlyings)
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"  !! failed {os.path.basename(path)}: {exc}")
            continue
        if not rows:
            print(f"  ~~ {d}: no matching underlyings in file")
            continue
        write_day(d, prefix, rows)
        written += 1
        total_rows += len(rows)
        print(f"  {d}  rows={len(rows):>5}  -> {os.path.relpath(out_path(d, prefix), STORE_ROOT)}")
    print(f"\nDONE. written={written} already_had={skipped} bad={bad} total_rows={total_rows}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", nargs="?", help="YYYY-MM-DD (omit with --from-zips)")
    ap.add_argument("end", nargs="?", help="YYYY-MM-DD (omit with --from-zips)")
    ap.add_argument("--source", choices=list(SOURCES), default="nse")
    ap.add_argument("--underlyings", default="", help="override (comma-sep); default = source's set")
    ap.add_argument("--overwrite", action="store_true", help="re-download days already stored")
    ap.add_argument("--from-zips", default=None, metavar="DIR",
                    help="ingest pre-downloaded bhavcopy .zip files from DIR instead of downloading "
                         "(for BSE/SENSEX, which is Akamai-gated: fetch the zips in a real browser, "
                         "drop them in DIR, then run this). Zip filenames must contain the 8-digit date.")
    args = ap.parse_args()

    src = SOURCES[args.source]
    prefix = src["prefix"]
    underlyings = ({u.strip().upper() for u in args.underlyings.split(",") if u.strip()}
                   or set(src["underlyings"]))

    if args.from_zips:
        _ingest_local_zips(args.from_zips, prefix, underlyings, args.overwrite)
        return

    if not args.start or not args.end:
        ap.error("start and end dates are required unless --from-zips is used")
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    print(f"source: {args.source}  store: {STORE_ROOT}")
    print(f"underlyings: {sorted(underlyings)}")
    print(f"range: {start} -> {end}\n")

    days = trading = holidays = skipped = total_rows = 0
    for d in daterange(start, end):
        days += 1
        if d.weekday() >= 5:  # Sat/Sun — skip without hitting the host
            continue
        if os.path.exists(out_path(d, prefix)) and not args.overwrite:
            skipped += 1
            continue
        blob = download(d, src)
        if blob is None:
            holidays += 1
            continue
        rows = parse_and_filter(blob, underlyings)
        if not rows:
            holidays += 1
            continue
        write_day(d, prefix, rows)
        trading += 1
        total_rows += len(rows)
        print(f"  {d}  rows={len(rows):>5}  -> {os.path.relpath(out_path(d, prefix), STORE_ROOT)}")
        time.sleep(0.4)  # be polite to the host

    print(f"\nDONE. days_scanned={days} written={trading} already_had={skipped} "
          f"non_trading_or_gated={holidays} total_rows={total_rows}")


if __name__ == "__main__":
    main()
