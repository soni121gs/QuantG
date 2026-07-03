"""Reader for the free EOD bhavcopy store (backend/scripts/bhavcopy_ingest.py).

Turns the gzipped per-day CSVs under data/bhavcopy_fo/<year>/ into the two
series a backtester needs:

  underlying_daily(u, start, end)  -> [{date, open, high, low, close, volume}]
       real daily OHLC of the near-month index FUTURE (≈ the index, good enough
       for signal generation). Date is stamped 'YYYY-MM-DD 15:25' so it looks
       like an EOD candle to strategy python_code.

  option_chain(u, date)            -> {expiry: {strike: {'CE': row, 'PE': row}}}
       every index-option contract's settle/close/oi for one trading day, used
       to price spread legs at entry / hold / exit.

Settlement price is the pricing basis (fair EOD value). Bhavcopy has no bid/ask,
so the backtester models slippage on top — see eod_options_backtest.py.
"""
from __future__ import annotations

import csv
import gzip
import glob
import logging
import os
from bisect import bisect_left
from datetime import date as _date
from functools import lru_cache
from typing import Any, Dict, List, Optional

logger = logging.getLogger("quantg.bhavcopy_store")

STORE_ROOT = os.environ.get(
    "BHAVCOPY_STORE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bhavcopy_fo"),
)


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class BhavcopyStore:
    def __init__(self, root: str = STORE_ROOT):
        self.root = root
        self._days: Optional[List[str]] = None  # cached sorted 'YYYY-MM-DD'

    # ---- day index -----------------------------------------------------------
    def trading_days(self, start: Optional[str] = None, end: Optional[str] = None) -> List[str]:
        if self._days is None:
            days = set()
            for path in glob.glob(os.path.join(self.root, "*", "*.csv.gz")):
                base = os.path.basename(path)
                digits = "".join(ch for ch in base if ch.isdigit())
                if len(digits) >= 8:
                    d = digits[-8:]
                    days.add(f"{d[:4]}-{d[4:6]}-{d[6:8]}")
            self._days = sorted(days)
        lo = bisect_left(self._days, start) if start else 0
        hi = bisect_left(self._days, end + "~") if end else len(self._days)
        return self._days[lo:hi]

    def _files_for(self, day: str) -> List[str]:
        yyyymmdd = day.replace("-", "")
        year = day[:4]
        return sorted(glob.glob(os.path.join(self.root, year, f"*{yyyymmdd}.csv.gz")))

    @lru_cache(maxsize=64)
    def load_day(self, day: str) -> tuple:
        """All rows for one trading day (NSE + BSE files merged). Cached; returns a
        tuple so it is hashable/immutable for the lru_cache."""
        rows: List[Dict[str, Any]] = []
        for path in self._files_for(day):
            try:
                with gzip.open(path, "rt") as f:
                    rows.extend(csv.DictReader(f))
            except Exception as exc:  # noqa: BLE001
                logger.warning("bhavcopy read failed %s: %s", path, exc)
        return tuple(rows)

    # ---- underlying OHLC (from near-month futures) ---------------------------
    def underlying_daily(self, underlying: str, start: Optional[str] = None,
                         end: Optional[str] = None) -> List[Dict[str, Any]]:
        u = underlying.upper()
        out: List[Dict[str, Any]] = []
        for day in self.trading_days(start, end):
            futs = [r for r in self.load_day(day)
                    if r.get("underlying") == u and r.get("instr_type") == "IDF"]
            if not futs:
                continue
            # near-month = nearest expiry >= day (fallback: earliest available)
            futs.sort(key=lambda r: r.get("expiry", ""))
            near = next((r for r in futs if r.get("expiry", "") >= day), futs[0])
            out.append({
                "date": f"{day} 15:25",
                "open": _f(near["open"]), "high": _f(near["high"]),
                "low": _f(near["low"]), "close": _f(near["close"]),
                "volume": int(_f(near["volume"])),
            })
        return out

    # ---- option chain for pricing -------------------------------------------
    def option_chain(self, underlying: str, day: str) -> Dict[str, Dict[float, Dict[str, Any]]]:
        u = underlying.upper()
        chain: Dict[str, Dict[float, Dict[str, Any]]] = {}
        for r in self.load_day(day):
            if r.get("underlying") != u or r.get("instr_type") != "IDO":
                continue
            typ = r.get("option_type")
            if typ not in ("CE", "PE"):
                continue
            exp = r.get("expiry", "")
            strike = _f(r.get("strike"))
            chain.setdefault(exp, {}).setdefault(strike, {})[typ] = r
        return chain

    def expiries(self, underlying: str, day: str) -> List[str]:
        return sorted(self.option_chain(underlying, day).keys())

    def leg_settle(self, underlying: str, day: str, expiry: str, strike: float,
                   opt_type: str) -> Optional[float]:
        """EOD settlement premium for one contract, or None if it did not trade /
        does not exist that day. Falls back to close if settle is blank."""
        chain = self.option_chain(underlying, day)
        node = chain.get(expiry, {}).get(strike, {})
        row = node.get(opt_type)
        if not row:
            return None
        px = _f(row.get("settle")) or _f(row.get("close"))
        return px if px > 0 else None
