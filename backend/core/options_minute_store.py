"""IMD-03 (write) + IMD-05 (read): the 1-minute options history store.

House format is gzipped CSV per contract-day (same as ``core/bhavcopy_store``) — no
new container dependency (pyarrow/duckdb are not installed and adding them is a
founder-gated rebuild). Layout mirrors the partition scheme in TASKS.md IMD-03:

    data/options_1m/
      source=upstox/
        underlying=NIFTY/
          year=2025/
            date=2025-01-09/
              24200CE_exp20250109.csv.gz          # one contract-day
              24200CE_exp20250109.csv.gz.manifest.json

Each row is the canonical schema from ``core/options_minute_schema``. The manifest
sidecar carries coverage + the deterministic checksum so a re-fetch of clean data
is a no-op (``is_clean``). The reader returns TYPED EMPTY results for missing
contracts — it never silently substitutes a nearest strike.
"""
from __future__ import annotations

import csv
import glob
import gzip
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.options_minute_schema import (
    OptionContractRef,
    build_manifest,
    manifest_checksum,
)

logger = logging.getLogger("quantg.options_minute_store")

IST = timezone(timedelta(hours=5, minutes=30))

STORE_ROOT = os.environ.get(
    "OPTIONS_1M_STORE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "options_1m"),
)

# NSE index-option session: 1-minute bars stamped 09:15 .. 15:29 (375 bars).
SESSION_START = (9, 15)
SESSION_END = (15, 29)

_CSV_FIELDS = (
    "timestamp_ist", "instrument_key", "expired_instrument_key", "underlying",
    "expiry", "strike", "option_type", "open", "high", "low", "close",
    "volume", "open_interest", "source", "fetched_at", "checksum",
)
_FLOAT_FIELDS = {"strike", "open", "high", "low", "close", "open_interest"}
_INT_FIELDS = {"volume"}


def _expiry_tag(expiry: str) -> str:
    return str(expiry).replace("-", "")[:8]


def expected_minutes(date: str) -> List[str]:
    """The 375 canonical IST bar timestamps for one trading date."""
    d = datetime.strptime(str(date)[:10], "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, SESSION_START[0], SESSION_START[1], tzinfo=IST)
    end = datetime(d.year, d.month, d.day, SESSION_END[0], SESSION_END[1], tzinfo=IST)
    out, cur = [], start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(minutes=1)
    return out


class OptionsMinuteStore:
    def __init__(self, root: str = STORE_ROOT):
        self.root = root

    # -- paths ----------------------------------------------------------------
    def _day_dir(self, source: str, underlying: str, date: str) -> str:
        year = str(date)[:4]
        return os.path.join(
            self.root, f"source={source}", f"underlying={underlying.upper()}",
            f"year={year}", f"date={str(date)[:10]}",
        )

    def _contract_file(self, source: str, underlying: str, date: str, expiry: str, strike: float, option_type: str) -> str:
        stem = f"{int(round(float(strike)))}{str(option_type).upper()}_exp{_expiry_tag(expiry)}.csv.gz"
        return os.path.join(self._day_dir(source, underlying, date), stem)

    @staticmethod
    def _manifest_path(contract_file: str) -> str:
        return contract_file + ".manifest.json"

    # -- write (IMD-03) -------------------------------------------------------
    def write_contract_day(self, candles: List[Dict[str, Any]], manifest: Dict[str, Any]) -> str:
        """Persist one contract-day + its manifest sidecar. Returns the file path."""
        source = manifest["source"]
        underlying = manifest["underlying"]
        date = str(manifest.get("first_timestamp") or manifest.get("from_date"))[:10]
        path = self._contract_file(
            source, underlying, date, manifest["expiry"], manifest["strike"], manifest["option_type"],
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = sorted(candles, key=lambda c: c["timestamp_ist"])
        with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=_CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        with open(self._manifest_path(path), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        return path

    def is_clean(self, source: str, underlying: str, date: str, expiry: str, strike: float, option_type: str) -> bool:
        """True iff the contract-day exists AND its manifest checksum matches content."""
        path = self._contract_file(source, underlying, date, expiry, strike, option_type)
        man_path = self._manifest_path(path)
        if not (os.path.exists(path) and os.path.exists(man_path)):
            return False
        try:
            with open(man_path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            return False
        rows = self._read_file(path)
        if not rows or len(rows) != manifest.get("row_count"):
            return False
        return manifest_checksum(rows) == manifest.get("checksum")

    # -- read (IMD-05) --------------------------------------------------------
    @staticmethod
    def _cast(row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        for f in _FLOAT_FIELDS:
            if f in out and out[f] not in (None, ""):
                out[f] = float(out[f])
        for f in _INT_FIELDS:
            if f in out and out[f] not in (None, ""):
                out[f] = int(float(out[f]))
        return out

    def _read_file(self, path: str) -> List[Dict[str, Any]]:
        if not os.path.exists(path):
            return []
        with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
            return [self._cast(r) for r in csv.DictReader(fh)]

    def get_option_minutes(
        self, source: str, underlying: str, date: str, expiry: str, strike: float, option_type: str
    ) -> List[Dict[str, Any]]:
        """All 1-minute candles for one contract-day, or [] if not stored."""
        path = self._contract_file(source, underlying, date, expiry, strike, option_type)
        return self._read_file(path)

    def _iter_day_files(self, source: str, underlying: str, date: str) -> List[str]:
        pattern = os.path.join(self._day_dir(source, underlying, date), "*.csv.gz")
        return sorted(glob.glob(pattern))

    def get_chain_at_time(
        self, source: str, underlying: str, date: str, timestamp_ist: str
    ) -> Dict[str, Dict[float, Dict[str, Dict[str, Any]]]]:
        """Reconstruct the option chain as of ``timestamp_ist`` (no lookahead).

        For each stored contract, take the last candle whose timestamp <= ts.
        Returns {expiry: {strike: {'CE'|'PE': candle}}}. Empty if nothing stored.
        """
        chain: Dict[str, Dict[float, Dict[str, Dict[str, Any]]]] = {}
        for path in self._iter_day_files(source, underlying, date):
            rows = [r for r in self._read_file(path) if r["timestamp_ist"] <= timestamp_ist]
            if not rows:
                continue
            last = rows[-1]
            exp = str(last["expiry"])
            strike = float(last["strike"])
            opt = str(last["option_type"]).upper()
            chain.setdefault(exp, {}).setdefault(strike, {})[opt] = last
        return chain

    def missing_minutes(
        self, source: str, underlying: str, date: str, expiry: str, strike: float, option_type: str
    ) -> List[str]:
        """Deterministic list of expected session minutes with no stored bar."""
        rows = self.get_option_minutes(source, underlying, date, expiry, strike, option_type)
        have = {r["timestamp_ist"] for r in rows}
        return [ts for ts in expected_minutes(date) if ts not in have]

    # -- coverage -------------------------------------------------------------
    def trading_days(self, source: str = "upstox", underlying: Optional[str] = None) -> List[str]:
        days = set()
        u_glob = f"underlying={underlying.upper()}" if underlying else "underlying=*"
        pattern = os.path.join(self.root, f"source={source}", u_glob, "year=*", "date=*")
        for path in glob.glob(pattern):
            base = os.path.basename(path)
            if base.startswith("date="):
                days.add(base[len("date="):])
        return sorted(days)

    def coverage(self, source: str = "upstox", underlying: Optional[str] = None) -> Dict[str, Any]:
        """Summary by date: contract count + row count per (underlying, date)."""
        by_day: Dict[str, Dict[str, Any]] = {}
        total_rows = 0
        total_files = 0
        underlyings = set()
        u_glob = f"underlying={underlying.upper()}" if underlying else "underlying=*"
        pattern = os.path.join(self.root, f"source={source}", u_glob, "year=*", "date=*", "*.csv.gz")
        for path in glob.glob(pattern):
            parts = path.replace("\\", "/").split("/")
            u = next((p[len("underlying="):] for p in parts if p.startswith("underlying=")), "?")
            d = next((p[len("date="):] for p in parts if p.startswith("date=")), "?")
            underlyings.add(u)
            key = f"{u}|{d}"
            n = sum(1 for _ in self._read_file(path))
            total_rows += n
            total_files += 1
            bucket = by_day.setdefault(key, {"underlying": u, "date": d, "contracts": 0, "rows": 0})
            bucket["contracts"] += 1
            bucket["rows"] += n
        days = sorted({v["date"] for v in by_day.values()})
        return {
            "source": source,
            "underlyings": sorted(underlyings),
            "days": days,
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None,
            "total_days": len(by_day),
            "total_contract_days": total_files,
            "total_rows": total_rows,
            "by_day": sorted(by_day.values(), key=lambda x: (x["underlying"], x["date"])),
        }
