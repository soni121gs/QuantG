"""Infra & data-integrity probes — feed / system / data-store health."""
from __future__ import annotations

from typing import Any, Dict, List

from core.hermes_diagnostics.contract import Finding, Domain, Severity
from core.hermes_diagnostics.probe_sdk import register, ProbeContext

# An index cannot move this far intraday; anything beyond is a bad tick / bad
# reference price (the −57.8% NIFTY "CRASH" flip seen on 2026-07-17).
_IMPOSSIBLE_INTRADAY_PCT = float(__import__("os").environ.get("HERMES_IMPOSSIBLE_MOVE_PCT", "20"))


@register("infra.feed_regime_artifact", kind="dynamic")
async def feed_regime_artifact(ctx: ProbeContext) -> List[Finding]:
    """Detect impossible intraday returns in the regime snapshots — a bad-tick /
    bad-reference-price signature that thrashes the regime classifier and can
    drive false stand-downs or entries."""
    worst: Dict[str, Dict[str, Any]] = {}
    for sig in ctx.signals_today:
        snap = sig.get("regime_snapshot") or {}
        idx = snap.get("index") or sig.get("symbol") or "?"
        try:
            ret = float(snap.get("intraday_return_pct"))
        except (TypeError, ValueError):
            continue
        if abs(ret) >= _IMPOSSIBLE_INTRADAY_PCT:
            cur = worst.get(idx)
            if cur is None or abs(ret) > abs(cur["intraday_return_pct"]):
                worst[idx] = {"index": idx, "intraday_return_pct": ret,
                              "computed_at": snap.get("computed_at"),
                              "regime": snap.get("regime")}
    out: List[Finding] = []
    for idx, ev in worst.items():
        out.append(Finding(
            probe_id="infra.feed_regime_artifact", domain=Domain.INFRA,
            severity=Severity.HIGH, entity=str(idx),
            title=f"{idx}: impossible intraday return {ev['intraday_return_pct']:.1f}% (bad tick)",
            detail=("A regime snapshot recorded an intraday return no index can make — "
                    "a corrupt tick or bad reference/expiry-roll price. This thrashes "
                    "the regime classifier (flipping to CRASH/MELTUP), which can drive "
                    "false stand-downs or off-regime entries. Same class as the RES-2 "
                    "realized-vol artifact."),
            evidence=ev,
            reproduction="scan db.signals[].regime_snapshot.intraday_return_pct for |x|>20 on the day",
            suggested_fix="Guard the regime realized-return input against non-contiguous / bad ticks (core/market_regime, entry_gate contiguity).",
        ))
    return out


@register("infra.overgated_book", kind="dynamic")
async def overgated_book(ctx: ProbeContext) -> List[Finding]:
    """Observational: the book fired many signals but took ~no trades because a
    single rejection reason dominated. Not necessarily a bug (stand-down IS a
    strategy) but worth surfacing so a mis-set gate that silently kills the book
    is visible rather than mistaken for 'a quiet day'."""
    if not ctx.signals_today:
        return []
    processed = [s for s in ctx.signals_today if str(s.get("status")) == "PROCESSED"]
    skipped = [s for s in ctx.signals_today if str(s.get("status")) == "SKIPPED_SIGNAL"]
    trades = (ctx.daily_report or {}).get("trades_taken", len(ctx.closed_today))
    if len(skipped) < 20 or trades > 0:
        return []
    reasons: Dict[str, int] = {}
    for s in skipped:
        r = str(s.get("rejection_reason") or "unknown")
        reasons[r] = reasons.get(r, 0) + 1
    top_reason, top_n = max(reasons.items(), key=lambda kv: kv[1])
    if top_n / max(1, len(skipped)) < 0.8:
        return []
    return [Finding(
        probe_id="infra.overgated_book", domain=Domain.INFRA, severity=Severity.LOW,
        entity="signal-gate",
        title=f"Book took 0 trades; {top_n}/{len(skipped)} signals skipped on '{top_reason}'",
        detail=("The book generated signals but placed no trades, with one gate "
                "accounting for nearly all rejections. If that gate is intended "
                "(regime stand-down) this is fine; if it is mis-set it is silently "
                "starving the book. Surfaced so it's a decision, not an accident."),
        evidence={"processed": len(processed), "skipped": len(skipped),
                  "trades_taken": trades, "dominant_reason": top_reason,
                  "reason_distribution": reasons},
        reproduction=("db.signals.aggregate([{$match:{status:'SKIPPED_SIGNAL',created_at:{$regex:'^%s'}}},"
                      "{$group:{_id:'$rejection_reason',n:{$sum:1}}}])" % ctx.date_str),
        suggested_fix="Confirm the dominant gate is intended for the day's regime; check router/quality/frequency gates.",
    )]
