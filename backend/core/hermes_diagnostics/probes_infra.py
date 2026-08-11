"""Infra & data-integrity probes — feed / system / data-store health."""
from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List

from core.hermes_diagnostics.contract import Finding, Domain, Severity
from core.hermes_diagnostics.probe_sdk import register, ProbeContext

# An index cannot move this far intraday; anything beyond is a bad tick / bad
# reference price (the −57.8% NIFTY "CRASH" flip seen on 2026-07-17).
_IMPOSSIBLE_INTRADAY_PCT = float(__import__("os").environ.get("HERMES_IMPOSSIBLE_MOVE_PCT", "20"))

# A book converting at or below this rate is effectively standing down, however many
# stray orders got through. One trade must not silence the over-gating probe.
_MAX_QUIET_CONVERSION = float(__import__("os").environ.get("HERMES_QUIET_CONVERSION", "0.02"))
_MIN_DOMINANCE = float(__import__("os").environ.get("HERMES_GATE_DOMINANCE", "0.6"))


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
    if len(skipped) < 20:
        return []
    # This probe was muted by `trades > 0` and a 0.8 dominance bar. On 2026-07-24 the
    # book fired 360 signals, took exactly ONE trade and skipped 279/359 (0.777) on a
    # single gate — the precise situation it exists to surface — and it stayed silent
    # on BOTH counts. Judge the ACTIVATION RATE, not a zero-trade absolute: a book
    # converting <2% of signals is standing down whether or not one order slipped out.
    conversion = len(processed) / max(1, len(ctx.signals_today))
    if conversion > _MAX_QUIET_CONVERSION:
        return []
    reasons: Dict[str, int] = {}
    for s in skipped:
        r = str(s.get("rejection_reason") or "unknown")
        reasons[r] = reasons.get(r, 0) + 1
    top_reason, top_n = max(reasons.items(), key=lambda kv: kv[1])
    if top_n / max(1, len(skipped)) < _MIN_DOMINANCE:
        return []
    return [Finding(
        probe_id="infra.overgated_book", domain=Domain.INFRA, severity=Severity.MEDIUM,
        entity="signal-gate",
        title=(f"Book converted {len(processed)}/{len(ctx.signals_today)} signals into trades; "
               f"{top_n}/{len(skipped)} skipped on '{top_reason}'"),
        detail=("The book generated signals but converted almost none into trades, with "
                "one gate accounting for most rejections. If that gate is intended "
                "(regime stand-down) this is fine; if it is mis-set it is silently "
                "starving the book. Surfaced so it's a decision, not an accident."),
        evidence={"processed": len(processed), "skipped": len(skipped),
                  "signals_total": len(ctx.signals_today),
                  "conversion_rate": round(conversion, 4),
                  "trades_taken": trades, "dominant_reason": top_reason,
                  "reason_distribution": reasons},
        reproduction=("db.signals.aggregate([{$match:{status:'SKIPPED_SIGNAL',created_at:{$regex:'^%s'}}},"
                      "{$group:{_id:'$rejection_reason',n:{$sum:1}}}])" % ctx.date_str),
        suggested_fix="Confirm the dominant gate is intended for the day's regime; check router/quality/frequency gates.",
    )]


@register("infra.process_restarts", kind="dynamic")
async def process_restarts(ctx: ProbeContext) -> List[Finding]:
    """The backend died and was restarted during the audited day.

    Added after 2026-07-23/24, when the nightly Edge Lab rebuild took uvicorn to
    9.9 GB and the kernel OOM-killer took the whole app down — seven times, once
    mid-session at 11:24 IST. Nothing recorded it, so every other probe graded a
    day whose process had been replaced under it. A restart wipes the in-memory
    1-minute capture buffers, so the regime classifier restarts from zero bars.
    """
    try:
        rows = await ctx.db.app_starts.find(
            {"started_at": {"$gte": ctx.date_str, "$lt": ctx.date_str + "~"}},
            {"_id": 0},
        ).to_list(100)
    except Exception:  # noqa: BLE001
        return []
    if len(rows) < 2:
        return []
    starts = sorted(str(r.get("started_at") or "") for r in rows)
    # 03:45–10:00 UTC == 09:15–15:30 IST. started_at is stored UTC.
    in_session = [s for s in starts if "03:45" <= s[11:16] <= "10:00"]
    if not in_session:
        return []
    sev = Severity.CRITICAL if in_session else Severity.HIGH
    return [Finding(
        probe_id="infra.process_restarts", domain=Domain.INFRA, severity=sev, entity="backend",
        title=(f"Backend restarted {len(rows) - 1}x today"
               + (f" — {len(in_session)} DURING market hours" if in_session else "")),
        detail=("The trading process did not survive the day. Each restart drops the "
                "Upstox feed, forces a full token resubscribe, and empties the live "
                "1-minute capture buffers — after which the regime classifier is "
                "working from a handful of bars and reports near-zero confidence. "
                "A mid-session restart means orders and marks were unattended. "
                "Check for an OOM kill (unbounded research caches) before reading "
                "any other finding from this day as a clean signal."),
        evidence={"starts": starts, "restarts": len(rows) - 1,
                  "in_market_hours": in_session},
        reproduction="db.app_starts.find({started_at:{$gte:'%s'}}) ; docker inspect -f '{{.RestartCount}}' quantg-backend ; dmesg -T | grep -i 'out of memory'" % ctx.date_str,
        suggested_fix=("Bound the research caches (BHAVCOPY_ROWS_CACHE_MB), keep heavy "
                       "walk-forwards out of the trading process, and set a container "
                       "mem_limit so a runaway job cannot take the host down."),
    )]


@register("data.capture_flush_failed", kind="dynamic")
async def capture_flush_failed(ctx: ProbeContext) -> List[Finding]:
    """The 15:35 IST 1-minute capture flush errored — today's forward data is lost.

    These failures used to be swallowed at debug (the store went 8 days stale), then
    logged at WARNING where the daily audit still could not see them. The scheduler
    now persists the outcome; this reads it.
    """
    try:
        doc = await ctx.db.capture_flush_runs.find_one({"_id": f"capture:{ctx.date_str}"})
    except Exception:  # noqa: BLE001
        return []
    if not doc:
        return []
    out: List[Finding] = []
    for kind in ("index", "options"):
        res = doc.get(kind) or {}
        err = res.get("error")
        failed = res.get("failed") or {}
        if not err and not failed:
            continue
        out.append(Finding(
            probe_id="data.capture_flush_failed", domain=Domain.DATA,
            severity=Severity.HIGH, entity=f"{kind}_1m",
            title=f"{kind} 1-minute capture flush failed for {ctx.date_str}",
            detail=("The day's captured minute bars were not written to the store, so "
                    "the intraday (IMD) judge is missing this session and any verdict "
                    "computed over it is graded on absent data. A filesystem "
                    "permission error here means the ./data mount is owned by a "
                    "different user than the container runs as."),
            evidence={"kind": kind, "error": err, "per_underlying_failures": failed,
                      "result": res},
            reproduction="db.capture_flush_runs.findOne({_id:'capture:%s'})" % ctx.date_str,
            suggested_fix=("Fix ownership of ./data for the container user (uid 999) and "
                           "make any host cron write as that user."),
        ))
    return out


@register("infra.feed_down_at_open", kind="dynamic")
async def feed_down_at_open(ctx: ProbeContext) -> List[Finding]:
    """The Upstox feed was not live at the market open — so the morning was never
    captured, whatever the capture code does downstream.

    Root cause found 2026-07-30: the daily Upstox token expires ~03:30 IST and needs a
    manual morning reconnect; when it lands late (that day, 11:58 IST) there is no valid
    token → no feed → index_1m/options_1m simply never see the 09:15–reconnect window.
    The scheduler's 09:20 IST watchdog records live/false into db.feed_open_status; this
    reads it so a silently-lost morning becomes a loud, on-record CRITICAL finding rather
    than a mysteriously short store day. Auto-resolves when a later day records live/true.
    """
    try:
        doc = await ctx.db.feed_open_status.find_one({"date": ctx.date_str})
    except Exception:  # noqa: BLE001
        return []
    if not doc or doc.get("live") is not False:
        return []
    # Best-effort: how much of the morning was lost (first captured index bar time).
    gap_note: Dict[str, Any] = {}
    try:
        flush = await ctx.db.capture_flush_runs.find_one({"_id": f"capture:{ctx.date_str}"})
        if flush:
            gap_note = {"index_bars_captured": (flush.get("index") or {}).get("bars"),
                        "option_bars_captured": (flush.get("options") or {}).get("bars_written")}
    except Exception:  # noqa: BLE001
        pass
    return [Finding(
        probe_id="infra.feed_down_at_open", domain=Domain.INFRA,
        severity=Severity.CRITICAL, entity="upstox-feed",
        title=f"Upstox feed was DOWN at the open on {ctx.date_str} — morning capture lost",
        detail=("The live feed was not connected at 09:15 IST, so the 1-minute index and "
                "option capture recorded nothing until the feed came up (usually a late "
                "daily token reconnect). The EOD backfill can heal NIFTY/BANKNIFTY index "
                "and option gaps from Upstox historical, but SENSEX options are not "
                "backfillable — that morning is gone. The fix is an earlier/automated "
                "token reconnect; this finding exists so late reconnects stop being silent."),
        evidence={"date": ctx.date_str, "reason": doc.get("reason"),
                  "recorded_at": doc.get("ts"), **gap_note},
        reproduction="db.feed_open_status.findOne({date:'%s'})" % ctx.date_str,
        suggested_fix=("Reconnect the Upstox token before 09:15 IST (automate it); until "
                       "then the EOD backfill heals NIFTY/BANKNIFTY but not SENSEX options."),
    )]


@register("data.store_writable", kind="static")
async def store_writable(ctx: ProbeContext) -> List[Finding]:
    """Can this process actually WRITE the research stores? Catches the permission
    class BEFORE the 15:35 flush loses a day, rather than after."""
    out: List[Finding] = []
    checks = []
    try:
        from core.index_minute_store import IndexMinuteStore
        checks.append(("index_1m", getattr(IndexMinuteStore(), "root", None)))
    except Exception:  # noqa: BLE001
        pass
    try:
        from core.options_minute_store import OptionsMinuteStore
        checks.append(("options_1m", getattr(OptionsMinuteStore(), "root", None)))
    except Exception:  # noqa: BLE001
        pass
    for name, root in checks:
        if not root or not os.path.isdir(root):
            continue
        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix=".hermes_wtest_"):
                pass
        except Exception as exc:  # noqa: BLE001
            out.append(Finding(
                probe_id="data.store_writable", domain=Domain.DATA,
                severity=Severity.HIGH, entity=name,
                title=f"{name}: store directory is NOT writable by this process",
                detail=("The live capture will fail at the 15:35 flush and the day's "
                        "minute bars will be lost. This is the root-owned ./data vs "
                        "container-user mismatch; it silently starves the intraday judge."),
                evidence={"store": name, "root": root,
                          "uid": getattr(os, "getuid", lambda: None)(),
                          "error": f"{type(exc).__name__}: {exc}"},
                reproduction=f"as the container user: touch {root}/.probe",
                suggested_fix=f"chown -R 999 {root} (and make any host cron write as uid 999).",
            ))
    return out
