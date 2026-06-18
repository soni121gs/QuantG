"""Phase 0 — per-strategy evaluation harness (the AutoResearch objective).

Aggregates the signal funnel + realised trade stats for each strategy over a
window, so "optimise every strategy" becomes a ranked, data-driven hit-list
instead of a guess. Pure reads over `signals` and `strategy_positions` — no
trading side effects.

Health classification answers the user's two fears directly:
  - SILENT   : generates ~no processed signals / no trades (not trading)
  - THROTTLED: lots of signals but most die in a filter (wants to trade, gated)
  - LOSSY    : trades, but net-negative P&L over the window
  - HEALTHY  : trades with non-negative P&L
  - IDLE     : paused/draft with no activity (expected, not a problem)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional


def _pnl_expr() -> Dict[str, Any]:
    return {"$ifNull": ["$realized_pnl", "$realised_pnl"]}


async def build_scorecard(db, *, days: int = 7, user_id: Optional[str] = None) -> Dict[str, Any]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Honor a declared clean-measurement baseline: never count trades/signals
    # from before it. This excludes since-fixed-bug history (pre-baseline
    # oversizing blowups, equity-phantom losses) from the alpha verdict without
    # deleting the raw records (audit trail preserved). Config doc is optional —
    # absent it, behaviour is the plain rolling window (back-compat).
    baseline_used: Optional[str] = None
    try:
        base_doc = await db.app_config.find_one({"_id": "research_baseline"})
        baseline = (base_doc or {}).get("baseline_date")
        if baseline and str(baseline) > cutoff:
            cutoff = str(baseline)
            baseline_used = str(baseline)
    except Exception:
        pass

    strat_filter: Dict[str, Any] = {}
    if user_id:
        strat_filter["user_id"] = user_id
    strategies = await db.strategies.find(
        strat_filter, {"_id": 0, "id": 1, "name": 1, "status": 1, "user_id": 1}
    ).to_list(2000)
    by_id: Dict[str, Dict[str, Any]] = {}
    for s in strategies:
        by_id[s["id"]] = {
            "strategy_id": s["id"],
            "name": s.get("name"),
            "status": s.get("status"),
            "signals_total": 0,
            "processed": 0,
            "filtered": 0,
            "skipped": 0,
            "filter_reasons": {},
            "trades": 0,
            "wins": 0,
            "total_pnl": 0.0,
            "worst_trade": 0.0,
        }

    # ── Signal funnel ────────────────────────────────────────────────────────
    sig_match: Dict[str, Any] = {"created_at": {"$gte": cutoff}}
    if user_id:
        sig_match["user_id"] = user_id
    sig_rows = await db.signals.aggregate([
        {"$match": sig_match},
        {"$group": {
            "_id": {"sid": "$strategy_id", "status": "$status", "reason": "$rejection_reason"},
            "n": {"$sum": 1},
        }},
    ]).to_list(100000)
    for row in sig_rows:
        sid = row["_id"].get("sid")
        rec = by_id.get(sid)
        if not rec:
            continue
        status = str(row["_id"].get("status") or "")
        n = int(row["n"])
        rec["signals_total"] += n
        if status == "PROCESSED":
            rec["processed"] += n
        elif status == "FILTERED":
            rec["filtered"] += n
            reason = str(row["_id"].get("reason") or "unknown")
            rec["filter_reasons"][reason] = rec["filter_reasons"].get(reason, 0) + n
        elif status.startswith("SKIPPED"):
            rec["skipped"] += n

    # ── Realised trade stats ─────────────────────────────────────────────────
    pos_match: Dict[str, Any] = {"closed_at": {"$gte": cutoff}}
    if user_id:
        pos_match["user_id"] = user_id
    pos_rows = await db.strategy_positions.aggregate([
        {"$match": pos_match},
        {"$group": {
            "_id": "$strategy_id",
            "trades": {"$sum": 1},
            "total_pnl": {"$sum": _pnl_expr()},
            "wins": {"$sum": {"$cond": [{"$gt": [_pnl_expr(), 0]}, 1, 0]}},
            "worst_trade": {"$min": _pnl_expr()},
        }},
    ]).to_list(100000)
    for row in pos_rows:
        rec = by_id.get(row["_id"])
        if not rec:
            continue
        rec["trades"] = int(row["trades"])
        rec["wins"] = int(row["wins"])
        rec["total_pnl"] = round(float(row["total_pnl"] or 0), 2)
        rec["worst_trade"] = round(float(row["worst_trade"] or 0), 2)

    # ── Derive per-strategy verdict ──────────────────────────────────────────
    cards: List[Dict[str, Any]] = []
    for rec in by_id.values():
        trades = rec["trades"]
        rec["win_rate"] = round(rec["wins"] / trades, 2) if trades else 0.0
        rec["avg_pnl"] = round(rec["total_pnl"] / trades, 2) if trades else 0.0
        filt_ratio = (rec["filtered"] / rec["signals_total"]) if rec["signals_total"] else 0.0
        rec["filtered_pct"] = round(filt_ratio * 100, 1)
        rec["top_filter_reason"] = (
            max(rec["filter_reasons"].items(), key=lambda kv: kv[1])[0]
            if rec["filter_reasons"] else None
        )

        active = str(rec["status"]) == "live"
        if trades > 0:
            health = "lossy" if rec["total_pnl"] < 0 else "healthy"
        elif rec["processed"] > 0:
            health = "healthy"  # processed signals but window had no closes yet
        elif rec["signals_total"] > 0 and filt_ratio >= 0.6:
            health = "throttled"
        elif active:
            health = "silent"
        else:
            health = "idle"
        rec["health"] = health
        cards.append(rec)

    # Rank: problems first (lossy worst, then throttled/silent), healthy last.
    order = {"lossy": 0, "throttled": 1, "silent": 2, "healthy": 3, "idle": 4}
    cards.sort(key=lambda c: (order.get(c["health"], 9), c["total_pnl"]))

    summary = {
        "window_days": days,
        "since": cutoff,
        "baseline_date": baseline_used,
        "strategies": len(cards),
        "by_health": {},
        "net_pnl": round(sum(c["total_pnl"] for c in cards), 2),
        "total_trades": sum(c["trades"] for c in cards),
    }
    for c in cards:
        summary["by_health"][c["health"]] = summary["by_health"].get(c["health"], 0) + 1

    return {"summary": summary, "cards": cards}
