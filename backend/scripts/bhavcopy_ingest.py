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
from datetime import date, datetime, timedelta

# EDR-05: the 10 equity-strategy stocks — the NSE cash (CM) bhavcopy carries their
# EOD OHLC so equity strategies can finally be backtested (index-only until now).
STOCK_FO_UNDERLYINGS = {"RELIANCE", "SBIN", "HDFCBANK", "ICICIBANK", "TCS", "INFY",
                        "AXISBANK", "LT", "BHARTIARTL", "KOTAKBANK"}
CM_UNDERLYINGS = set(STOCK_FO_UNDERLYINGS)
FO_INSTR_TYPES = {"IDO", "IDF", "STO", "STF"}

# --- source registry ----------------------------------------------------------
SOURCES = {
    "nse": {
        "url": "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip",
        "underlyings": {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"} | STOCK_FO_UNDERLYINGS,
        "prefix": "BhavCopy_FO_",           # keep legacy NSE filename (494 files already stored)
        "referer": "https://www.nseindia.com/",
        "kind": "fo", "store": "bhavcopy_fo",
    },
    "cm": {
        # NSE cash-market (equity) EOD bhavcopy — per-stock OHLC. Not gated (urllib works).
        "url": "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d}_F_0000.csv.zip",
        "underlyings": set(CM_UNDERLYINGS),
        "prefix": "BhavCopy_CM_",
        "referer": "https://www.nseindia.com/",
        "kind": "cm", "store": "bhavcopy_cm",
    },
    "bse": {
        # BSE now serves the F&O UDiFF bhavcopy as a PLAIN .CSV (not zipped), and
        # it is Akamai-gated — use bse_bhavcopy_download.js on a real desktop, then
        # `--from-zips ./bse_csv`. This direct URL is kept for reference/parity.
        "url": "https://www.bseindia.com/download/BhavCopy/Derivative/BhavCopy_BSE_FO_0_0_0_{d}_F_0000.CSV",
        "underlyings": {"SENSEX", "BANKEX", "SENSEX50"},
        "prefix": "BhavCopy_BSE_FO_",
        "referer": "https://www.bseindia.com/markets/Derivatives/DeriReports.aspx",
        "kind": "fo", "store": "bhavcopy_fo",
    },
}

OLD_NSE_FO_URL = os.environ.get(
    "OLD_NSE_FO_URL",
    "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{yyyy}/{mon}/fo{dd}{mon}{yyyy}bhav.csv.zip",
)
OLD_NSE_CUTOFF = date(2024, 1, 1)

STORE_ROOT = os.environ.get(
    "BHAVCOPY_STORE",
    # …/backend/data/bhavcopy_fo — must match core/bhavcopy_store.py's default
    # (both resolve to /app/data/bhavcopy_fo in the container, the ./data bind mount).
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bhavcopy_fo"),
)
DATA_ROOT = os.path.dirname(STORE_ROOT)   # …/data — CM store is a sibling of bhavcopy_fo


def store_root_for(src: dict) -> str:
    return os.path.join(DATA_ROOT, src.get("store", "bhavcopy_fo"))


# Equity (CM) store columns — per-stock EOD OHLC (no strike/expiry/settle).
CM_OUT_COLS = ["date", "symbol", "series", "open", "high", "low", "close",
               "prev_close", "volume", "value"]


def parse_cm_csv(raw: str, underlyings: set[str]) -> list[dict]:
    """Parse an NSE CM (cash/equity) UDiFF bhavcopy → per-stock EOD OHLC rows,
    filtered to the EQ series and the requested tickers."""
    reader = csv.DictReader(io.StringIO(raw))
    out = []
    for row in reader:
        if (row.get("SctySrs") or "").strip() != "EQ":   # equity series only
            continue
        sym = row.get("TckrSymb", "")
        if underlyings and sym not in underlyings:
            continue
        out.append({
            "date": row.get("TradDt", ""), "symbol": sym, "series": "EQ",
            "open": row.get("OpnPric", ""), "high": row.get("HghPric", ""),
            "low": row.get("LwPric", ""), "close": row.get("ClsPric", ""),
            "prev_close": row.get("PrvsClsgPric", ""),
            "volume": row.get("TtlTradgVol", ""), "value": row.get("TtlTrfVal", ""),
        })
    return out
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Clean, backtester-friendly subset of the 34-col UDiFF file. instr_type keeps
# IDO (option) vs IDF (future) apart; futures rows have blank strike/option_type.
OUT_COLS = [
    "date", "instr_type", "underlying", "expiry", "strike", "option_type", "symbol",
    "open", "high", "low", "close", "settle", "underlying_price",
    "oi", "chg_in_oi", "volume", "lot_size",
]


def out_path(d: date, prefix: str, store_dir: str = STORE_ROOT) -> str:
    return os.path.join(store_dir, str(d.year), f"{prefix}{d.strftime('%Y%m%d')}.csv.gz")


def download(d: date, src: dict, retries: int = 3) -> bytes | None:
    """Return raw zip bytes, or None on 404 / non-zip (holiday or gated host)."""
    url = src["url"].format(d=d.strftime("%Y%m%d"))
    if src.get("kind") == "fo" and src.get("prefix") == "BhavCopy_FO_" and d < OLD_NSE_CUTOFF:
        mon = d.strftime("%b").upper()
        url = OLD_NSE_FO_URL.format(
            d=d.strftime("%Y%m%d"), yyyy=d.strftime("%Y"), mon=mon,
            dd=d.strftime("%d"), mm=d.strftime("%m"),
        )
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


def _instr_types(raw: str) -> set[str]:
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def parse_and_filter(zip_bytes: bytes, underlyings: set[str], kind: str = "fo",
                     instr_types: set[str] | None = None) -> list[dict]:
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    raw = z.read(z.namelist()[0]).decode("utf-8", "replace")
    if kind == "cm":
        return parse_cm_csv(raw, underlyings)
    if "INSTRUMENT,SYMBOL,EXPIRY_DT" in raw[:200]:
        return parse_legacy_fo_csv(raw, underlyings, instr_types or FO_INSTR_TYPES)
    return parse_udiff_csv(raw, underlyings, instr_types or FO_INSTR_TYPES)


def parse_udiff_csv(raw: str, underlyings: set[str], instr_types: set[str] | None = None) -> list[dict]:
    """Parse a raw UDiFF bhavcopy CSV (NSE or BSE — same common format). BSE now
    serves the F&O bhavcopy as a plain .CSV (not zipped)."""
    allowed = instr_types or {"IDO", "IDF"}
    reader = csv.DictReader(io.StringIO(raw))
    out = []
    for row in reader:
        itp = row.get("FinInstrmTp")
        if itp not in allowed:
            continue
        sym = row.get("TckrSymb", "")
        if underlyings and sym not in underlyings:
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


def parse_legacy_fo_csv(raw: str, underlyings: set[str], instr_types: set[str] | None = None) -> list[dict]:
    """Parse pre-2024 NSE F&O bhavcopy files into the normalized UDiFF-shaped rows."""
    allowed = instr_types or FO_INSTR_TYPES
    type_map = {"FUTIDX": "IDF", "OPTIDX": "IDO", "FUTSTK": "STF", "OPTSTK": "STO"}
    reader = csv.DictReader(io.StringIO(raw))
    out = []
    for row in reader:
        instr = (row.get("INSTRUMENT") or "").strip().upper()
        itp = type_map.get(instr)
        if not itp or itp not in allowed:
            continue
        sym = (row.get("SYMBOL") or "").strip().upper()
        if underlyings and sym not in underlyings:
            continue
        typ = (row.get("OPTION_TYP") or "").strip().upper()
        if typ == "XX":
            typ = ""
        out.append({
            "date": _legacy_date(row.get("TIMESTAMP")),
            "instr_type": itp,
            "underlying": sym,
            "expiry": _legacy_date(row.get("EXPIRY_DT")),
            "strike": "" if itp in {"IDF", "STF"} else row.get("STRIKE_PR", ""),
            "option_type": typ,
            "symbol": _legacy_symbol(row),
            "open": row.get("OPEN", ""),
            "high": row.get("HIGH", ""),
            "low": row.get("LOW", ""),
            "close": row.get("CLOSE", ""),
            "settle": row.get("SETTLE_PR", ""),
            "underlying_price": row.get("CLOSE", "") if itp in {"IDF", "STF"} else "",
            "oi": row.get("OPEN_INT", ""),
            "chg_in_oi": row.get("CHG_IN_OI", ""),
            "volume": row.get("CONTRACTS", ""),
            "lot_size": "",
        })
    return out


def _legacy_date(raw: str | None) -> str:
    s = str(raw or "").strip()
    for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s[:10]


def _legacy_symbol(row: dict) -> str:
    sym = (row.get("SYMBOL") or "").strip().upper()
    exp = _legacy_date(row.get("EXPIRY_DT"))
    strike = str(row.get("STRIKE_PR") or "").strip()
    typ = (row.get("OPTION_TYP") or "").strip().upper()
    if typ and typ != "XX":
        return f"{sym} {strike} {typ} {exp}"
    return f"{sym} FUT {exp}"


def write_day(d: date, prefix: str, rows: list[dict],
              cols: list[str] = OUT_COLS, store_dir: str = STORE_ROOT) -> None:
    path = out_path(d, prefix, store_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _ingest_local_zips(zip_dir: str, prefix: str, underlyings: set[str], overwrite: bool,
                       kind: str = "fo", cols: list[str] = OUT_COLS,
                       store_dir: str = STORE_ROOT,
                       instr_types: set[str] | None = None) -> None:
    """Parse pre-downloaded bhavcopy .zip files (any source) into the gz store.
    Filenames must contain the 8-digit trading date (YYYYMMDD). Robust to
    non-zip files (skips them). Use this for BSE SENSEX/BANKEX, which the
    automated downloader cannot reach (Akamai bot-gate)."""
    import glob as _glob
    import re as _re
    files = sorted(set(
        _glob.glob(os.path.join(zip_dir, "*.zip")) + _glob.glob(os.path.join(zip_dir, "*.ZIP"))
        + _glob.glob(os.path.join(zip_dir, "*.csv")) + _glob.glob(os.path.join(zip_dir, "*.CSV"))
    ))
    print(f"ingest-from-files: {len(files)} file(s) in {zip_dir}")
    print(f"underlyings: {sorted(underlyings)}  store: {STORE_ROOT}\n")
    written = skipped = bad = total_rows = 0
    for path in files:
        m = _re.search(r"(\d{8})", os.path.basename(path))
        if not m:
            print(f"  ?? no date in filename, skipped: {os.path.basename(path)}")
            continue
        g = m.group(1)
        # filenames may be YYYYMMDD or DDMMYYYY — detect by which end is a valid year
        y = g[:4] if g[:2] in ("19", "20") else g[4:8]
        mo, dy = (g[4:6], g[6:8]) if g[:2] in ("19", "20") else (g[2:4], g[0:2])
        d = date(int(y), int(mo), int(dy))
        if os.path.exists(out_path(d, prefix, store_dir)) and not overwrite:
            skipped += 1
            continue
        try:
            with open(path, "rb") as f:
                blob = f.read()
            _csv_parse = parse_cm_csv if kind == "cm" else (
                lambda text, names: parse_udiff_csv(text, names, instr_types or FO_INSTR_TYPES)
            )
            if blob[:2] == b"PK":                       # zipped bhavcopy
                rows = parse_and_filter(blob, underlyings, kind, instr_types)
            elif blob[:4] in (b"Trad", b"\xef\xbb\xbfT"):  # plain UDiFF CSV (BSE now serves this)
                rows = _csv_parse(blob.decode("utf-8", "replace"), underlyings)
            else:
                bad += 1
                print(f"  !! not a bhavcopy (HTML/error page?), skipped: {os.path.basename(path)}")
                continue
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"  !! failed {os.path.basename(path)}: {exc}")
            continue
        if not rows:
            print(f"  ~~ {d}: no matching underlyings in file")
            continue
        write_day(d, prefix, rows, cols, store_dir)
        written += 1
        total_rows += len(rows)
        print(f"  {d}  rows={len(rows):>5}  -> {os.path.relpath(out_path(d, prefix, store_dir), store_dir)}")
    print(f"\nDONE. written={written} already_had={skipped} bad={bad} total_rows={total_rows}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", nargs="?", help="YYYY-MM-DD (omit with --from-zips)")
    ap.add_argument("end", nargs="?", help="YYYY-MM-DD (omit with --from-zips)")
    ap.add_argument("--source", choices=list(SOURCES), default="nse")
    ap.add_argument("--underlyings", default="", help="override (comma-sep); default = source's set")
    ap.add_argument("--all-underlyings", action="store_true",
                    help="keep EVERY F&O underlying in the bhavcopy (all ~180 stocks + indices), "
                         "not just the source's curated set. Use to backfill the full stock-option "
                         "universe for the earnings/event sleeves.")
    ap.add_argument("--instr-types", default="",
                    help="F&O instrument types, comma-separated; default includes IDO,IDF,STO,STF")
    ap.add_argument("--overwrite", action="store_true", help="re-download days already stored")
    ap.add_argument("--from-zips", default=None, metavar="DIR",
                    help="ingest pre-downloaded bhavcopy .zip files from DIR instead of downloading "
                         "(for BSE/SENSEX, which is Akamai-gated: fetch the zips in a real browser, "
                         "drop them in DIR, then run this). Zip filenames must contain the 8-digit date.")
    args = ap.parse_args()

    src = SOURCES[args.source]
    prefix = src["prefix"]
    kind = src.get("kind", "fo")
    store_dir = store_root_for(src)
    cols = CM_OUT_COLS if kind == "cm" else OUT_COLS
    # --all-underlyings → empty set = "keep everything the parser sees" (guarded by
    # the instr-types filter, so still only F&O). Otherwise: explicit list, else the
    # source's curated set.
    if args.all_underlyings:
        underlyings: set = set()
    else:
        underlyings = ({u.strip().upper() for u in args.underlyings.split(",") if u.strip()}
                       or set(src["underlyings"]))
    instr_types = _instr_types(args.instr_types) or (FO_INSTR_TYPES if kind == "fo" else set())

    if args.from_zips:
        _ingest_local_zips(args.from_zips, prefix, underlyings, args.overwrite, kind, cols, store_dir, instr_types)
        return

    if not args.start or not args.end:
        ap.error("start and end dates are required unless --from-zips is used")
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    print(f"source: {args.source}  store: {STORE_ROOT}")
    print(f"underlyings: {sorted(underlyings)}")
    if kind == "fo":
        print(f"instr_types: {sorted(instr_types)}")
    print(f"range: {start} -> {end}\n")

    days = trading = holidays = skipped = total_rows = 0
    for d in daterange(start, end):
        days += 1
        if d.weekday() >= 5:  # Sat/Sun — skip without hitting the host
            continue
        if os.path.exists(out_path(d, prefix, store_dir)) and not args.overwrite:
            skipped += 1
            continue
        blob = download(d, src)
        if blob is None:
            holidays += 1
            continue
        rows = parse_and_filter(blob, underlyings, kind, instr_types)
        if not rows:
            holidays += 1
            continue
        write_day(d, prefix, rows, cols, store_dir)
        trading += 1
        total_rows += len(rows)
        print(f"  {d}  rows={len(rows):>5}  -> {os.path.relpath(out_path(d, prefix, store_dir), store_dir)}")
        time.sleep(0.4)  # be polite to the host

    print(f"\nDONE. days_scanned={days} written={trading} already_had={skipped} "
          f"non_trading_or_gated={holidays} total_rows={total_rows}")


if __name__ == "__main__":
    main()
