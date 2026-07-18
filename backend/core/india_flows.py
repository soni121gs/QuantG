"""IA-4 (2026-07-11): free India-specific flow data — FII/DII daily net cash flows
from NSE's public API. FII (foreign) net buying/selling is the highest-conviction
independent regime signal for NIFTY (positioning of the marginal price-setter),
and — unlike the options OI we just proved dead — it is a DIRECTIONAL flow, not
seller positioning.

Legal/data rule (CLAUDE.md §14.1): NSE's own public endpoint under normal terms,
no scraping of gated/paid sources. The VPS is India-resident so NSE is reachable.

This module FETCHES + STORES + READS. Historical OOS validation needs a backfill
(the live API returns only recent days); forward capture starts accumulating now.
Store mirrors the bhavcopy pattern: gzipped JSONL under data/india_flows/.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

STORE_ROOT = os.environ.get(
    "INDIA_FLOWS_ROOT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "india_flows"),
)
PARTICIPANT_OI_ROOT = os.environ.get(
    "PARTICIPANT_OI_ROOT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "participant_oi"),
)
_NSE_HOME = "https://www.nseindia.com"
_FIIDII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _opener() -> urllib.request.OpenerDirector:
    """NSE sets an Akamai cookie on the home page that its API requires."""
    cj_opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor())
    cj_opener.addheaders = [("User-Agent", _UA), ("Accept", "*/*"),
                            ("Accept-Language", "en-US,en;q=0.9")]
    try:
        cj_opener.open(_NSE_HOME, timeout=12).read()      # prime cookies
    except Exception:
        pass
    return cj_opener


def fetch_fiidii() -> List[Dict[str, Any]]:
    """Return the API's recent FII/DII rows, normalized to:
        {date(YYYY-MM-DD), fii_net, dii_net, fii_buy, fii_sell, dii_buy, dii_sell}
    (net in ₹ crore). Raises on hard failure; returns [] on empty."""
    op = _opener()
    req = urllib.request.Request(_FIIDII_URL, headers={
        "User-Agent": _UA, "Accept": "application/json",
        "Referer": "https://www.nseindia.com/reports/fii-dii"})
    raw = op.open(req, timeout=12).read()
    data = json.loads(raw)
    rows = data if isinstance(data, list) else data.get("data") or []
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            d = _norm_date(r.get("date"))
            cat = str(r.get("category") or "").upper()
            row = {"date": d,
                   "buy": float(r.get("buyValue") or 0),
                   "sell": float(r.get("sellValue") or 0),
                   "net": float(r.get("netValue") or 0),
                   "category": cat}
            out.append(row)
        except (TypeError, ValueError):
            continue
    # collapse the FII + DII rows for a date into one record
    by_date: Dict[str, Dict[str, Any]] = {}
    for r in out:
        rec = by_date.setdefault(r["date"], {"date": r["date"]})
        pfx = "fii" if "FII" in r["category"] or "FPI" in r["category"] else "dii"
        rec[f"{pfx}_buy"] = r["buy"]
        rec[f"{pfx}_sell"] = r["sell"]
        rec[f"{pfx}_net"] = r["net"]
    return list(by_date.values())


def _norm_date(raw: Any) -> str:
    s = str(raw or "").strip()
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s[:10]


def _path(date: str) -> str:
    y = date[:4]
    return os.path.join(STORE_ROOT, y, f"fiidii_{date}.json.gz")


def store_rows(rows: List[Dict[str, Any]]) -> int:
    """Idempotent per-day write. Returns count written."""
    n = 0
    for r in rows:
        d = r.get("date")
        if not d or len(d) != 10:
            continue
        p = _path(d)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        r = {**r, "_ingested_at": datetime.now(timezone.utc).isoformat()}
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps(r))
        n += 1
    return n


def get_flows(date: str) -> Optional[Dict[str, Any]]:
    p = _path(date)
    if not os.path.exists(p):
        return None
    with gzip.open(p, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def available_days() -> List[str]:
    import glob
    out = []
    for p in glob.glob(os.path.join(STORE_ROOT, "*", "fiidii_*.json.gz")):
        base = os.path.basename(p)
        out.append(base[len("fiidii_"):-len(".json.gz")])
    return sorted(out)


def parse_participant_oi_csv(raw: str) -> List[Dict[str, Any]]:
    """Parse NSE participant-wise F&O OI CSV into normalized rows.

    The source has changed headers over time, so matching is deliberately
    tolerant while the output names stay stable for probes/judges.
    """
    reader = csv.DictReader(io.StringIO(raw))
    out: List[Dict[str, Any]] = []
    for row in reader:
        lowered = {str(k).strip().lower(): v for k, v in row.items()}
        client_type = (
            lowered.get("client type") or lowered.get("participant") or lowered.get("category") or ""
        ).strip()
        if not client_type or client_type.lower() in {"total", "grand total"}:
            continue
        rec = {"participant": client_type.upper()}
        for key, value in lowered.items():
            name = (
                key.replace(" ", "_").replace("-", "_").replace("/", "_")
                .replace("(", "").replace(")", "").replace("__", "_")
            )
            if name in {"client_type", "participant", "category"}:
                continue
            try:
                rec[name] = float(str(value).replace(",", "")) if str(value).strip() else 0.0
            except ValueError:
                rec[name] = value
        out.append(rec)
    return out


def _participant_path(date: str) -> str:
    return os.path.join(PARTICIPANT_OI_ROOT, date[:4], f"participant_oi_{date}.json.gz")


def store_participant_oi(date: str, rows: List[Dict[str, Any]], source: str = "nse") -> int:
    if not date or len(date) != 10:
        return 0
    path = _participant_path(date)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "date": date,
        "source": source,
        "rows": rows,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True))
    return len(rows)


def get_participant_oi(date: str) -> Optional[Dict[str, Any]]:
    path = _participant_path(date)
    if not os.path.exists(path):
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def participant_oi_days() -> List[str]:
    import glob
    out = []
    for p in glob.glob(os.path.join(PARTICIPANT_OI_ROOT, "*", "participant_oi_*.json.gz")):
        base = os.path.basename(p)
        out.append(base[len("participant_oi_"):-len(".json.gz")])
    return sorted(out)
