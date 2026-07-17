"""Strategy & edge-integrity probes — persistent loss and sample-size honesty."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from core.hermes_diagnostics.contract import Finding, Domain, Severity
from core.hermes_diagnostics.probe_sdk import register, ProbeContext

_LOOKBACK_DAYS = 15
_MIN_TRADES_FOR_LOSS_CALL = 15
_MIN_TRADES_FOR_EVIDENCE = 30   # CLAUDE.md law: nothing is 'working' under 30 trades


@register("strategy.persistent_live_loss", kind="dynamic")
async def persistent_live_loss(ctx: ProbeContext) -> List[Finding]:
    """Flag any strategy that is net-negative over a meaningful recent sample —
    a candidate for review/removal, not silent continuation. Deterministic sum of
    realized P&L over the trailing window from closed positions."""
    since = (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    cursor = ctx.db.strategy_positions.aggregate([
        {"$match": {"user_id": ctx.user_id, "status": "CLOSED",
                    "created_at": {"$gte": since}}},
        {"$group": {"_id": "$strategy_id",
                    "pnl": {"$sum": "$realized_pnl"},
                    "trades": {"$sum": 1},
                    "wins": {"$sum": {"$cond": [{"$gt": ["$realized_pnl", 0]}, 1, 0]}}}},
    ])
    rows = await cursor.to_list(200)
    out: List[Finding] = []
    for r in rows:
        sid = str(r.get("_id"))
        pnl = float(r.get("pnl") or 0.0)
        trades = int(r.get("trades") or 0)
        if trades < _MIN_TRADES_FOR_LOSS_CALL or pnl >= 0:
            continue
        strat = ctx.strategy_by_id(sid) or {}
        wr = (r.get("wins", 0) / trades) if trades else 0
        out.append(Finding(
            probe_id="strategy.persistent_live_loss", domain=Domain.STRATEGY,
            severity=Severity.MEDIUM, entity=sid,
            title=f"Net-negative over {trades} trades (₹{pnl:,.0f}, {wr*100:.0f}% WR)",
            detail=(f"This strategy has lost money over the last {_LOOKBACK_DAYS} days on "
                    "a sample large enough to be signal, not noise. It is a candidate "
                    "for review or removal rather than continued paper-forward — grade "
                    "the idea on its expectancy, not hope."),
            evidence={"strategy_id": sid, "name": strat.get("name"),
                      "window_days": _LOOKBACK_DAYS, "trades": trades,
                      "realized_pnl": round(pnl, 2), "win_rate": round(wr, 3)},
            reproduction=("db.strategy_positions.aggregate group by strategy_id since %s, sum realized_pnl" % since),
            suggested_fix="Review the strategy's OOS/forward-paper evidence; archive if the edge is absent (CLAUDE.md §13).",
        ))
    return out


@register("strategy.thin_sample_grading", kind="dynamic")
async def thin_sample_grading(ctx: ProbeContext) -> List[Finding]:
    """Honesty check: an ACTIVE strategy still under the 30-trade evidence floor
    is being run on faith, not proof. Low severity — a label, not an alarm."""
    cursor = ctx.db.strategy_positions.aggregate([
        {"$match": {"user_id": ctx.user_id, "status": "CLOSED"}},
        {"$group": {"_id": "$strategy_id", "trades": {"$sum": 1}}},
    ])
    counts = {str(r["_id"]): int(r["trades"]) for r in await cursor.to_list(500)}
    out: List[Finding] = []
    for strat in ctx.strategies:
        if str(strat.get("status")) not in ("live", "active"):
            continue
        sid = str(strat.get("id"))
        n = counts.get(sid, 0)
        if n >= _MIN_TRADES_FOR_EVIDENCE:
            continue
        out.append(Finding(
            probe_id="strategy.thin_sample_grading", domain=Domain.STRATEGY,
            severity=Severity.LOW, entity=sid,
            title=f"Active on a thin sample ({n}/{_MIN_TRADES_FOR_EVIDENCE} trades)",
            detail=("This strategy is active but has fewer than 30 closed trades, so "
                    "its live P&L is noise, not evidence. Keep it in forward-paper; do "
                    "not treat green/red days as proof either way."),
            evidence={"strategy_id": sid, "name": strat.get("name"), "closed_trades": n},
            reproduction="count CLOSED strategy_positions per strategy_id",
            suggested_fix="No fix — a label. Promote only after 30+ trades AND positive OOS/forward-paper.",
        ))
    return out
