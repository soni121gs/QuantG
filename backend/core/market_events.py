"""RES-2 signal E — the market EVENT-RISK GATE (fat-tail gate).

Don't sell cheap insurance right before a known bomb.

This module does NOT predict a move and does NOT read news. It answers one
deterministic question — "is this date a scheduled fat-tail day?" — from a table
of KNOWN events (expiry, RBI/Fed/Budget, results) and returns a continuous size
multiplier the caller applies to its position sizing.

Three design laws it obeys:

  • PURE + live/historical parity. Same function, same answer for a 2024 date and
    for today, so a strategy gated by it stays OOS-validatable. That is the
    non-negotiable RES rule: a signal that cannot be reconstructed historically
    can never be validated (§15.3).

  • CONTINUOUS size, not a binary block. Per the EdgeMath mandate (§16) an event
    day fades size toward zero instead of hard-blocking, so the book de-risks
    smoothly rather than flipping on/off.

  • NEVER fabricate dates. Macro dates (RBI MPC / Budget / FOMC) are LOADED from a
    verified file — never asserted from memory (§20 verify-live-facts law, the
    lesson from the lot-size incident). Expiry dates are passed in by the caller
    from real instrument/store data rather than derived from a hardcoded weekday
    rule, because exchange expiry cycles change.

Storage mirrors `core/earnings_calendar`: one JSONL file per year under
`data/market_events/`, appended by an ingest and read here. An empty store simply
means "no known events" — the gate then returns size_mult 1.0 (fail-open).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

STORE_ROOT = os.environ.get(
    "MARKET_EVENTS_ROOT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "market_events"),
)

# Event kinds.
KIND_EXPIRY = "EXPIRY"
KIND_MACRO = "MACRO"        # RBI MPC, Union Budget, FOMC, CPI…
KIND_EARNINGS = "EARNINGS"  # a single stock's results day

# Continuous size multiplier per severity level. HIGH ≈ stand down, MEDIUM ≈ trade
# small, LOW ≈ normal. Env-tunable so the founder can soften/harden without a
# deploy, and so a sweep can test the levels as parameters.
_LEVEL_SIZE: Dict[str, float] = {
    "HIGH": float(os.environ.get("EVENT_RISK_SIZE_HIGH", "0.0")),
    "MEDIUM": float(os.environ.get("EVENT_RISK_SIZE_MEDIUM", "0.35")),
    "LOW": float(os.environ.get("EVENT_RISK_SIZE_LOW", "1.0")),
}
_LEVEL_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
_DEFAULT_LEVEL_BY_KIND = {KIND_EXPIRY: "HIGH", KIND_MACRO: "HIGH", KIND_EARNINGS: "HIGH"}


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
    """Coerce an ingest row into the canonical event shape.

    `underlying=None` means MARKET-WIDE (applies to every symbol). `lead_days`
    lets a high-impact event also flag the day(s) before it — you want to be flat
    going INTO the event, not scrambling on the morning of.
    """
    date = _norm_date(row.get("date") or row.get("event_date"))
    kind = str(row.get("kind") or KIND_MACRO).upper()
    level = str(row.get("level") or _DEFAULT_LEVEL_BY_KIND.get(kind, "MEDIUM")).upper()
    if level not in _LEVEL_SIZE:
        level = "MEDIUM"
    und = row.get("underlying")
    try:
        lead = max(0, int(row.get("lead_days") or 0))
    except (TypeError, ValueError):
        lead = 0
    return {
        "date": date,
        "name": str(row.get("name") or row.get("event") or kind),
        "kind": kind,
        "level": level,
        "underlying": (str(und).upper() if und else None),
        "lead_days": lead,
        "source": str(row.get("source") or "manual"),
    }


def store_events(rows: Iterable[Dict[str, Any]]) -> int:
    """Append normalized events, de-duped on (date, name, underlying)."""
    by_year: Dict[str, List[Dict[str, Any]]] = {}
    for raw in rows:
        ev = normalize_event(raw)
        if not ev["date"] or len(ev["date"]) != 10:
            continue
        by_year.setdefault(ev["date"][:4], []).append(ev)

    written = 0
    for year, evs in by_year.items():
        p = _path(year)
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = {(e["date"], e["name"], e.get("underlying")): e for e in _load_year(year)}
        for e in evs:
            existing[(e["date"], e["name"], e.get("underlying"))] = e
        with p.open("w", encoding="utf-8") as fh:
            for e in sorted(existing.values(), key=lambda x: (x["date"], x["name"])):
                fh.write(json.dumps(e) + "\n")
                written += 1
    return written


def _load_year(year: str) -> List[Dict[str, Any]]:
    p = _path(year)
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    except Exception:  # noqa: BLE001 — a corrupt file must not break trading
        return []
    return out


def events_on(date_str: str, *, underlying: Optional[str] = None) -> List[Dict[str, Any]]:
    """Known events ACTIVE on `date_str` — including events whose `lead_days`
    window reaches this date. Market-wide events always apply; symbol-specific
    events apply only to that symbol."""
    d = _norm_date(date_str)
    if len(d) != 10:
        return []
    u = str(underlying).upper() if underlying else None
    try:
        target = datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        return []

    hits: List[Dict[str, Any]] = []
    # An event dated later can still cover today via its lead window, so scan this
    # year and the next (a lead window never spans more than a few days).
    for year in {d[:4], str(int(d[:4]) + 1)}:
        for e in _load_year(year):
            if e.get("underlying") and e["underlying"] != u:
                continue
            try:
                ed = datetime.strptime(e["date"], "%Y-%m-%d").date()
            except (ValueError, KeyError):
                continue
            lead = int(e.get("lead_days") or 0)
            if ed - timedelta(days=lead) <= target <= ed:
                hits.append({**e, "days_until": (ed - target).days})
    return sorted(hits, key=lambda e: (-_LEVEL_RANK.get(e.get("level", "MEDIUM"), 1), e["date"]))


def event_risk(
    date_str: str,
    *,
    underlying: Optional[str] = None,
    expiry_dates: Optional[Sequence[str]] = None,
    extra_events: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """THE gate. Returns the event-risk verdict for a date.

    PURE: every input is passed in or read from the on-disk table, so the live
    path and the backtester get identical answers.

    `expiry_dates` — real expiry dates from the instrument master (live) or the
    bhavcopy store (historical). Passed in, never derived from a weekday rule.
    `extra_events` — caller-supplied rows (e.g. earnings for a stock underlying)
    merged with the stored table.

    Returns {date, level, size_mult, is_event, is_expiry, events, reasons}.
    size_mult multiplies the caller's position size: 1.0 = normal, 0.0 = stand down.
    """
    d = _norm_date(date_str)
    events = list(events_on(d, underlying=underlying))

    if expiry_dates and d in {str(x)[:10] for x in expiry_dates}:
        events.append({"date": d, "name": "expiry", "kind": KIND_EXPIRY,
                       "level": _DEFAULT_LEVEL_BY_KIND[KIND_EXPIRY],
                       "underlying": (str(underlying).upper() if underlying else None),
                       "lead_days": 0, "source": "instrument_master", "days_until": 0})
    for raw in (extra_events or []):
        ev = normalize_event(raw)
        if ev["date"] == d and (not ev["underlying"] or ev["underlying"] == (str(underlying).upper() if underlying else None)):
            events.append({**ev, "days_until": 0})

    if not events:
        return {"date": d, "level": "NONE", "size_mult": 1.0, "is_event": False,
                "is_expiry": False, "events": [], "reasons": ["no known event"]}

    # Worst level wins — the fattest tail sets the size.
    level = max((e.get("level", "MEDIUM") for e in events), key=lambda l: _LEVEL_RANK.get(l, 1))
    size_mult = _LEVEL_SIZE.get(level, 1.0)
    reasons = [f"{e.get('kind')}: {e.get('name')}"
               + (f" (in {e['days_until']}d)" if e.get("days_until") else "")
               for e in events]
    return {
        "date": d,
        "level": level,
        "size_mult": size_mult,
        "is_event": any(e.get("kind") != KIND_EXPIRY for e in events),
        "is_expiry": any(e.get("kind") == KIND_EXPIRY for e in events),
        "events": events,
        "reasons": reasons,
    }


def available_days() -> List[str]:
    """Distinct dates present in the store — for coverage/freshness probes."""
    days: set = set()
    root = Path(STORE_ROOT)
    if not root.exists():
        return []
    for p in sorted(root.glob("*.jsonl")):
        for e in _load_year(p.stem):
            if e.get("date"):
                days.add(e["date"])
    return sorted(days)
