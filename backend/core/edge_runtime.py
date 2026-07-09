"""EdgeMath I/O adapters shared by live execution and historical validation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core.edge_sizer import RollingStats, rolling_stats_from_pnls
from core.market_context import build_context

IST = timezone(timedelta(hours=5, minutes=30))


def _bucket(value: Any, fallback: str = "UNKNOWN") -> str:
    return str(value or fallback).upper()


def context_dimensions(context: Optional[Dict[str, Any]]) -> Dict[str, str]:
    context = context or {}
    return {
        "regime": _bucket((context.get("regime") or {}).get("regime")),
        "vol_state": _bucket((context.get("vol_state") or {}).get("state")),
    }


async def rolling_edge_stats(
    db,
    *,
    user_id: str,
    strategy_id: str,
    regime: str,
    vol_state: str,
    limit: int = 60,
) -> RollingStats:
    """Return the latest closed-trade P&Ls for one strategy/context bucket."""
    query = {
        "user_id": user_id,
        "strategy_id": strategy_id,
        "regime_at_entry": _bucket(regime),
        "vol_state_at_entry": _bucket(vol_state),
    }
    rows = await (
        db.trade_attribution.find(query, {"_id": 0, "realized_pnl": 1})
        .sort("exit_time", -1)
        .limit(max(1, int(limit)))
        .to_list(max(1, int(limit)))
    )
    if not rows:
        query.pop("vol_state_at_entry")
        rows = await (
            db.trade_attribution.find(query, {"_id": 0, "realized_pnl": 1})
            .sort("exit_time", -1)
            .limit(max(1, int(limit)))
            .to_list(max(1, int(limit)))
        )
    return rolling_stats_from_pnls([float(row.get("realized_pnl") or 0) for row in reversed(rows)])


async def cached_rolling_edge_stats(
    db,
    *,
    user_id: str,
    strategy_id: str,
    regime: str,
    vol_state: str,
    limit: int = 60,
) -> RollingStats:
    date = datetime.now(IST).date().isoformat()
    key = f"{date}:{user_id}:{strategy_id}:{_bucket(regime)}:{_bucket(vol_state)}:{int(limit)}"
    cached = await db.edge_stats_cache.find_one({"_id": key}, {"_id": 0, "stats": 1})
    if cached:
        return RollingStats(**cached["stats"])
    stats = await rolling_edge_stats(
        db, user_id=user_id, strategy_id=strategy_id, regime=regime,
        vol_state=vol_state, limit=limit,
    )
    await db.edge_stats_cache.update_one(
        {"_id": key},
        {"$set": {"stats": stats.__dict__, "date_ist": date, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return stats


def build_market_context(*, source: str, **inputs: Any) -> Dict[str, Any]:
    """One explicit adapter entry point for live-feed and historical-store callers."""
    if source not in {"live", "historical"}:
        raise ValueError("source must be live or historical")
    return build_context(source=source, **inputs)


async def compile_edge_advice(db, user_id: str, *, limit: int = 60) -> Dict[str, Any]:
    """Observe-only EOD ratchet suggestions; never mutates strategy config."""
    strategies = await db.strategies.find(
        {"user_id": user_id}, {"_id": 0, "id": 1, "name": 1},
    ).to_list(500)
    rows = []
    for strategy in strategies:
        trades = await db.trade_attribution.find(
            {"user_id": user_id, "strategy_id": strategy["id"]},
            {"_id": 0, "realized_pnl": 1},
        ).sort("exit_time", -1).limit(limit).to_list(limit)
        stats = rolling_stats_from_pnls([float(r.get("realized_pnl") or 0) for r in reversed(trades)])
        rows.append({
            "strategy_id": strategy["id"],
            "strategy_name": strategy.get("name"),
            "n": stats.n,
            "expectancy": stats.expectancy,
            "suggested_risk_mult": round(max(0.25, min(1.25, 1.0 + stats.expectancy / 2000.0)), 3),
            "status": "observe_only",
        })
    doc = {
        "user_id": user_id,
        "generated_at": datetime.now(timezone.utc),
        "enabled": False,
        "rows": rows,
    }
    await db.edge_math_advice.update_one({"user_id": user_id}, {"$set": doc}, upsert=True)
    return doc
