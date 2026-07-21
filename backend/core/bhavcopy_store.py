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

# Per-(underlying, day) row cache. Sized to hold a full walk-forward for one
# underlying (~1860 days) so a backtest parses each day once instead of thrashing.
# Entries are a single underlying's slice (~2-3% of the file), so this is far
# cheaper in memory than caching whole days.
_ROWS_FOR_CACHE = int(os.environ.get("BHAVCOPY_ROWS_CACHE", "4096"))

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
        tuple so it is hashable/immutable for the lru_cache.

        NOTE: prefer `rows_for(underlying, day)` in any per-underlying path (every
        backtest). Since the full stock universe was ingested a day-file carries
        ~30k rows and this builds a dict for ALL of them; a backtest then discards
        ~97%. Use this only when you genuinely need every underlying (coverage).
        """
        rows: List[Dict[str, Any]] = []
        for path in self._files_for(day):
            try:
                with gzip.open(path, "rt") as f:
                    rows.extend(csv.DictReader(f))
            except Exception as exc:  # noqa: BLE001
                logger.warning("bhavcopy read failed %s: %s", path, exc)
        return tuple(rows)

    @lru_cache(maxsize=_ROWS_FOR_CACHE)
    def rows_for(self, underlying: str, day: str) -> tuple:
        """Rows for ONE underlying on ONE day — filtered DURING the scan.

        This is the hot path for every backtest. `load_day` builds a dict for all
        ~30k rows in the file; here we read with csv.reader and only materialise a
        dict for rows that MATCH, so we allocate ~2-3% as many objects. Combined
        with a cache sized for a full walk-forward (load_day's 64-day cache thrashes
        over 1860 days), a given (underlying, day) is parsed once and then reused by
        every strategy and by both the futures and option-chain paths.

        The durable fix is a per-underlying slim store written at ingest time; this
        removes the worst of the tax without changing the on-disk layout.
        """
        u = underlying.upper()
        out: List[Dict[str, Any]] = []
        for path in self._files_for(day):
            try:
                with gzip.open(path, "rt") as f:
                    reader = csv.reader(f)
                    header = next(reader, None)
                    if not header:
                        continue
                    try:
                        ui = header.index("underlying")
                    except ValueError:  # unexpected layout — fall back to full parse
                        out.extend(r for r in csv.DictReader(gzip.open(path, "rt"))
                                   if r.get("underlying") == u)
                        continue
                    for row in reader:
                        if len(row) > ui and row[ui] == u:
                            out.append(dict(zip(header, row)))
            except Exception as exc:  # noqa: BLE001
                logger.warning("bhavcopy read failed %s: %s", path, exc)
        return tuple(out)

    # ---- underlying OHLC (from near-month futures) ---------------------------
    @lru_cache(maxsize=16)
    def underlying_daily(self, underlying: str, start: Optional[str] = None,
                         end: Optional[str] = None) -> List[Dict[str, Any]]:
        u = underlying.upper()
        out: List[Dict[str, Any]] = []
        for day in self.trading_days(start, end):
            futs = [r for r in self.rows_for(u, day)
                    if r.get("instr_type") in ("IDF", "STF")]
            if not futs:
                continue
            # near-month = nearest expiry >= day (fallback: earliest available)
            futs.sort(key=lambda r: r.get("expiry", ""))
            near = next((r for r in futs if r.get("expiry", "") >= day), futs[0])
            # Stamp the single daily bar at a midday time so strategies' intraday
            # time-of-day gates (e.g. 09:30–14:15) pass — at daily granularity that
            # clock filter is a no-op by design; we evaluate the daily trend/regime
            # once per day. Intraday VWAP (which resets per day) degenerates to the
            # bar's typical price and is effectively neutral here.
            out.append({
                "date": f"{day} 11:00",
                "open": _f(near["open"]), "high": _f(near["high"]),
                "low": _f(near["low"]), "close": _f(near["close"]),
                "volume": int(_f(near["volume"])),
            })
        return out

    # ---- option chain for pricing -------------------------------------------
    @lru_cache(maxsize=16)
    def option_chain(self, underlying: str, day: str) -> Dict[str, Dict[float, Dict[str, Any]]]:
        u = underlying.upper()
        chain: Dict[str, Dict[float, Dict[str, Any]]] = {}
        for r in self.rows_for(u, day):
            if r.get("instr_type") not in ("IDO", "STO"):
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
        """EOD option premium (mark) for one contract. Returns None only when the
        contract is absent that day; returns 0.0 for a contract that expired
        worthless (a valid price).

        Uses `close` (last traded premium) as the primary mark. On EXPIRY day
        NSE's UDiFF overwrites the option `settle` column with the UNDERLYING
        settlement price (not the premium), so `settle` is only trusted as a
        fallback when it is a plausible option premium (>0 and < spot)."""
        chain = self.option_chain(underlying, day)
        row = chain.get(expiry, {}).get(strike, {}).get(opt_type)
        if not row:
            return None
        close = _f(row.get("close"))
        if close > 0:
            return close
        settle = _f(row.get("settle"))
        spot = _f(row.get("underlying_price"))
        if 0 < settle < spot:            # reject the expiry-day spot placeholder
            return settle
        return 0.0                        # traded/settled worthless

    def leg_lot_size(self, underlying: str, day: str, expiry: str, strike: float,
                     opt_type: str) -> Optional[int]:
        row = self.option_chain(underlying, day).get(expiry, {}).get(strike, {}).get(opt_type)
        if not row:
            return None
        lot = int(_f(row.get("lot_size")))
        return lot or None
