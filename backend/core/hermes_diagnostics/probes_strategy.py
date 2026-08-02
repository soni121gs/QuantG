"""Strategy & edge-integrity probes — persistent loss and sample-size honesty."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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
        # Split the sample at the strategy's last MATERIAL geometry change. A
        # trailing loss is only evidence against the CURRENT machine; if the
        # geometry was re-cut two days ago, the older trades measure a strategy
        # that no longer exists. This never resolves or downgrades the finding —
        # it stays open at the same severity until the post-change sample is both
        # large enough AND positive — it only stops a stale number from reading as
        # a verdict on the current configuration.
        epoch = await _epoch_split(ctx, sid, strat)
        title = f"Net-negative over {trades} trades (₹{pnl:,.0f}, {wr*100:.0f}% WR)"
        detail = (f"This strategy has lost money over the last {_LOOKBACK_DAYS} days on "
                  "a sample large enough to be signal, not noise. It is a candidate "
                  "for review or removal rather than continued paper-forward — grade "
                  "the idea on its expectancy, not hope.")
        evidence = {"strategy_id": sid, "name": strat.get("name"),
                    "window_days": _LOOKBACK_DAYS, "trades": trades,
                    "realized_pnl": round(pnl, 2), "win_rate": round(wr, 3)}
        if epoch:
            evidence.update(epoch)
            n_post = epoch["trades_since_change"]
            title += f" — {n_post} of them since the {epoch['geometry_changed_at'][:10]} re-cut"
            detail += (
                f"\n\nSAMPLE SPLIT: the geometry was re-cut on "
                f"{epoch['geometry_changed_at'][:10]} ({epoch.get('geometry_change_note') or 'config change'}). "
                f"{epoch['trades_before_change']} of these trades ran the OLD geometry "
                f"(₹{epoch['pnl_before_change']:,.0f}); {n_post} have run the new one "
                f"(₹{epoch['pnl_since_change']:,.0f}). "
                + ("The post-change sample is still too thin to judge — this finding stays "
                   "open until it reaches the evidence floor, and the pre-change loss is "
                   "NOT proof the current geometry is bad."
                   if n_post < _MIN_TRADES_FOR_LOSS_CALL else
                   "The post-change sample is now large enough to judge on its own — read it, "
                   "not the blended number."))
        out.append(Finding(
            probe_id="strategy.persistent_live_loss", domain=Domain.STRATEGY,
            severity=Severity.MEDIUM, entity=sid,
            title=title,
            detail=detail,
            evidence=evidence,
            reproduction=("db.strategy_positions.aggregate group by strategy_id since %s, sum realized_pnl" % since),
            suggested_fix="Review the strategy's OOS/forward-paper evidence; archive if the edge is absent (CLAUDE.md §13).",
        ))
    return out


async def _epoch_split(ctx: ProbeContext, sid: str, strat: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Split this strategy's trailing trades either side of its last material
    geometry change (`geometry_changed_at`, stamped by the re-cut migrations).

    Returns None when the strategy has never been re-cut, so untouched strategies
    keep the plain blended verdict.
    """
    changed_at = strat.get("geometry_changed_at")
    if not changed_at:
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    cursor = ctx.db.strategy_positions.aggregate([
        {"$match": {"user_id": ctx.user_id, "status": "CLOSED", "strategy_id": sid,
                    "created_at": {"$gte": since}}},
        {"$group": {"_id": {"$gte": ["$created_at", changed_at]},
                    "pnl": {"$sum": "$realized_pnl"}, "trades": {"$sum": 1}}},
    ])
    buckets = {bool(r["_id"]): r for r in await cursor.to_list(10)}
    post, pre = buckets.get(True, {}), buckets.get(False, {})
    return {
        "geometry_changed_at": str(changed_at),
        "geometry_change_note": strat.get("geometry_change_note"),
        "trades_since_change": int(post.get("trades") or 0),
        "pnl_since_change": round(float(post.get("pnl") or 0.0), 2),
        "trades_before_change": int(pre.get("trades") or 0),
        "pnl_before_change": round(float(pre.get("pnl") or 0.0), 2),
    }


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


@register("strategy.score_not_predictive", kind="dynamic")
async def score_not_predictive(ctx: ProbeContext) -> List[Finding]:
    """M5 (2026-08-02): flag any pre-trade SCORE whose information coefficient vs
    realized P&L is non-predictive (DECORATION) or inverted (INVERTED) — sizing that
    leans on such a score is sizing on noise. Reads the latest db.score_ic_runs
    written by scripts/run_score_ic.py. Silent when no run exists (thin evidence →
    silence, §19). INVERTED is HIGH (actively harmful); DECORATION is MEDIUM."""
    run = await ctx.db.score_ic_runs.find_one({}, {"_id": 0}, sort=[("generated_at", -1)])
    if not run:
        return []
    out: List[Finding] = []
    for r in run.get("results", []):
        verdict = str(r.get("verdict"))
        if verdict not in ("DECORATION", "INVERTED"):
            continue
        out.append(Finding(
            probe_id="strategy.score_not_predictive", domain=Domain.STRATEGY,
            severity=Severity.HIGH if verdict == "INVERTED" else Severity.MEDIUM,
            entity=str(r.get("name")),
            title=f"Score '{r.get('name')}' is {verdict} (IC {r.get('ic')}, t {r.get('t_stat')})",
            detail=("This pre-trade score does not positively predict realized P&L "
                    f"(information coefficient {r.get('ic')}, t={r.get('t_stat')}, "
                    f"n={r.get('n')}). Any position sizing that scales with its magnitude "
                    "is sizing on noise (or, if INVERTED, backwards). Neutralised by "
                    "SCORE_SIZE_NEUTRAL=true; keep it neutral until the score earns a "
                    "positive, significant IC."),
            evidence={"score": r.get("name"), "verdict": verdict, "ic": r.get("ic"),
                      "t_stat": r.get("t_stat"), "n": r.get("n"),
                      "generated_at": run.get("generated_at")},
            reproduction="scripts/run_score_ic.py → db.score_ic_runs latest; IC = Spearman(score, realized_pnl)",
            suggested_fix="Keep SCORE_SIZE_NEUTRAL=true so the score cannot scale size; investigate/repair or retire the score.",
        ))
    return out
