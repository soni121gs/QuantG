"""File-backed earnings calendar for ERP event studies.

Rows are stored as JSONL under ``data/earnings_dates/<year>.jsonl`` and queried
by symbol/date range. The store is intentionally simple so public NSE/manual
backfills can feed the same deterministic event-conditional judge later.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


STORE_ROOT = os.environ.get(
    "EARNINGS_DATES_ROOT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "earnings_dates"),
)


def _norm_date(raw: Any) -> str:
    s = str(raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s[:10]


def _path(year: str) -> Path:
    return Path(STORE_ROOT) / f"{year}.jsonl"


def normalize_event(row: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(row.get("symbol") or row.get("TckrSymb") or row.get("ticker") or "").upper().strip()
    event_date = _norm_date(row.get("date") or row.get("event_date") or row.get("results_date"))
    return {
        "symbol": symbol,
        "date": event_date,
        "event_type": str(row.get("event_type") or "earnings").lower(),
        "source": row.get("source") or "manual",
        "headline": row.get("headline") or row.get("company_name") or symbol,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


def store_events(rows: Iterable[Dict[str, Any]]) -> int:
    by_year: Dict[str, Dict[tuple, Dict[str, Any]]] = {}
    for row in rows:
        event = normalize_event(row)
        if not event["symbol"] or len(event["date"]) != 10:
            continue
        bucket = by_year.setdefault(event["date"][:4], {})
        bucket[(event["symbol"], event["date"], event["event_type"])] = event
    written = 0
    for year, rows_by_key in by_year.items():
        path = _path(year)
        existing: Dict[tuple, Dict[str, Any]] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    existing[(rec.get("symbol"), rec.get("date"), rec.get("event_type"))] = rec
        existing.update(rows_by_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for rec in sorted(existing.values(), key=lambda r: (r["date"], r["symbol"], r["event_type"])):
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
                written += 1
    return written


def events_for(symbol: str, start: str, end: Optional[str] = None) -> List[Dict[str, Any]]:
    symbol = symbol.upper()
    end = end or start
    years = range(int(start[:4]), int(end[:4]) + 1)
    out: List[Dict[str, Any]] = []
    for year in years:
        path = _path(str(year))
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("symbol") == symbol and start <= rec.get("date", "") <= end:
                    out.append(rec)
    return sorted(out, key=lambda r: r["date"])


def available_days() -> List[str]:
    out = set()
    for path in Path(STORE_ROOT).glob("*.jsonl"):
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    out.add(json.loads(line).get("date", ""))
    return sorted(d for d in out if d)
