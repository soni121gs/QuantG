"""Read-only whole-portfolio intelligence for Hermes and the Ops UI.

This module deliberately derives a snapshot from persisted positions and fills;
it does not mark positions, reserve capital, route orders, or change strategy
state.  Missing fields remain explicit instead of being guessed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from core.portfolio_risk import book_heat, position_risk


OPEN_STATUSES = {"OPEN", "FILLED", "EXITING", "PENDING_OPEN", "PENDING_BROKER", "RESERVED"}


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _freshness(rows: Iterable[Dict[str, Any]], now: datetime, fields: tuple[str, ...]) -> Dict[str, Any]:
    parsed = [dt for row in rows for dt in (_as_utc(next((row.get(field) for field in fields if row.get(field)), None)),) if dt]
    latest = max(parsed) if parsed else None
    return {"status": "AVAILABLE" if latest else "UNKNOWN", "latest": latest.isoformat() if latest else None,
            "age_seconds": round(max(0.0, (now - latest).total_seconds()), 1) if latest else None,
            "source_timestamps": len(parsed)}


def _greek(pos: Dict[str, Any], name: str) -> float | None:
    for source in (pos.get("greeks"), pos.get("greeks_at_signal"), pos):
        if isinstance(source, dict) and source.get(name) is not None:
            try:
                return float(source[name])
            except (TypeError, ValueError):
                return None
    return None


def _bucket(rows: Iterable[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "name": "UNKNOWN", "positions": 0, "risk": 0.0, "unrealized_pnl": 0.0,
        "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0,
    })
    for pos in rows:
        name = str(pos.get(key) or "UNKNOWN").upper()
        item = grouped[name]
        item["name"] = name
        item["positions"] += 1
        item["risk"] += position_risk(pos)
        item["unrealized_pnl"] += _num(pos.get("pnl") if pos.get("pnl") is not None else pos.get("unrealized_pnl"))
        for greek in ("delta", "gamma", "theta", "vega"):
            value = _greek(pos, greek)
            if value is not None:
                item[greek] += value
    return [
        {**item, "risk": round(item["risk"], 2), "unrealized_pnl": round(item["unrealized_pnl"], 2),
         **{g: round(item[g], 6) for g in ("delta", "gamma", "theta", "vega")}}
        for item in sorted(grouped.values(), key=lambda x: x["risk"], reverse=True)
    ]


def _pnl_bucket(rows: Iterable[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"name": "UNKNOWN", "fills": 0, "realized_pnl": 0.0})
    for row in rows:
        name = str(row.get(key) or "UNKNOWN").upper()
        item = grouped[name]
        item["name"] = name
        item["fills"] += 1
        item["realized_pnl"] += _num(row.get("realized_pnl") if row.get("realized_pnl") is not None else row.get("pnl"))
    return [{**item, "realized_pnl": round(item["realized_pnl"], 2)} for item in sorted(grouped.values(), key=lambda x: x["realized_pnl"], reverse=True)]


def build_portfolio_snapshot(positions: List[Dict[str, Any]], fills: List[Dict[str, Any]], *, now: datetime | None = None) -> Dict[str, Any]:
    """Build a deterministic, read-only whole-book snapshot from persisted rows."""
    now = now or datetime.now(timezone.utc)
    open_rows = [p for p in positions if str(p.get("status") or "").upper() in OPEN_STATUSES]
    realized = round(sum(_num(f.get("realized_pnl") if f.get("realized_pnl") is not None else f.get("pnl")) for f in fills), 2)
    unrealized = round(sum(_num(p.get("pnl") if p.get("pnl") is not None else p.get("unrealized_pnl")) for p in open_rows), 2)
    greek_coverage = {g: sum(1 for p in open_rows if _greek(p, g) is not None) for g in ("delta", "gamma", "theta", "vega")}
    by_underlying = _bucket(open_rows, "underlying")
    total_risk = book_heat(open_rows)
    alerts: List[Dict[str, Any]] = []
    if total_risk > 0 and by_underlying and by_underlying[0]["risk"] / total_risk >= 0.5:
        lead = by_underlying[0]
        alerts.append({"severity": "warning", "code": "UNDERLYING_CONCENTRATION", "title": "Risk is concentrated", "detail": f"{lead['name']} carries {lead['risk'] / total_risk:.0%} of defined risk.", "evidence": {"underlying": lead["name"], "risk": lead["risk"], "total_risk": total_risk}})
    missing_greeks = [g for g, n in greek_coverage.items() if n < len(open_rows)]
    if missing_greeks and open_rows:
        alerts.append({"severity": "info", "code": "GREEK_COVERAGE_INCOMPLETE", "title": "Greek coverage is incomplete", "detail": f"Missing persisted coverage for {', '.join(missing_greeks)}.", "evidence": {"missing": missing_greeks, "positions": len(open_rows)}})
    if realized + unrealized < 0:
        alerts.append({"severity": "warning", "code": "BOOK_PNL_NEGATIVE", "title": "Book P&L is negative", "detail": f"Realized plus unrealized P&L is {realized + unrealized:.2f}.", "evidence": {"realized_pnl": realized, "unrealized_pnl": unrealized}})
    return {
        "as_of": now.isoformat(),
        "source": {"positions": "db.strategy_positions", "fills": "db.trade_fills"},
        "read_only": True,
        "open_positions": len(open_rows),
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "total_pnl": round(realized + unrealized, 2),
        "defined_risk": round(book_heat(open_rows), 2),
        "greeks": {g: {"value": round(sum(_greek(p, g) or 0.0 for p in open_rows), 6), "covered_positions": greek_coverage[g], "total_positions": len(open_rows)} for g in ("delta", "gamma", "theta", "vega")},
        "by_underlying": by_underlying,
        "by_strategy": _bucket(open_rows, "strategy_id"),
        "realized_by_strategy": _pnl_bucket(fills, "strategy_id"),
        "realized_by_underlying": _pnl_bucket(fills, "underlying"),
        "risk_alerts": alerts,
        "data_quality": {"greek_coverage": greek_coverage, "missing_greeks": missing_greeks,
                          "freshness": {"positions": _freshness(open_rows, now, ("updated_at", "marked_at", "created_at")),
                                        "fills": _freshness(fills, now, ("filled_at", "created_at", "updated_at"))}},
        "note": "Read-only derived view. Missing marks or Greeks are reported, never inferred.",
    }


async def load_portfolio_snapshot(db: Any, user_id: str, *, now: datetime | None = None) -> Dict[str, Any]:
    positions = await db.strategy_positions.find({"user_id": user_id}, {"_id": 0, "user_id": 0}).to_list(5000)
    fills = await db.trade_fills.find({"user_id": user_id}, {"_id": 0, "user_id": 0}).to_list(10000)
    return build_portfolio_snapshot(positions, fills, now=now)
