"""The diagnostic runner (Layers 2+3+5).

Builds a shared read-only context, runs every registered probe, persists findings
to `db.hermes_findings` with dedup + lifecycle, and auto-resolves findings that a
fix has made disappear. Deterministic — no LLM here. The narrator (Layer 4) runs
separately over the CONFIRMED findings this returns.

Collections:
  hermes_findings         one doc per (probe, entity) key; status open|resolved
  hermes_diagnostic_runs  one doc per run: counts + the narrative (filled by narrator)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.hermes_diagnostics.contract import Finding, Severity, Confidence
from core.hermes_diagnostics import probe_sdk
from core.hermes_diagnostics.probe_sdk import ProbeContext, run_probe_safe

# Import probe modules for their @register side effects.
from core.hermes_diagnostics import (  # noqa: F401
    probes_static, probes_execution, probes_infra, probes_strategy, probes_data,
)

logger = logging.getLogger("quantg.hermes_diagnostics")

_IST = timezone(timedelta(hours=5, minutes=30))


async def _build_context(db, user_id: str, date_str: str, now: Optional[datetime]) -> ProbeContext:
    now = now or datetime.now(timezone.utc)
    ist = now.astimezone(_IST)
    hm = f"{ist.hour:02d}:{ist.minute:02d}"
    in_hours = (ist.weekday() < 5) and ("09:15" <= hm <= "15:30")

    strategies = await db.strategies.find({"user_id": user_id}).to_list(500)
    closed_today = await db.strategy_positions.find(
        {"user_id": user_id, "status": "CLOSED",
         "$or": [{"created_at": {"$regex": f"^{date_str}"}},
                 {"opened_at": {"$regex": f"^{date_str}"}},
                 {"closed_at": {"$regex": f"^{date_str}"}}]}
    ).to_list(1000)
    open_positions = await db.strategy_positions.find(
        {"user_id": user_id, "status": {"$in": ["OPEN", "FILLED", "PENDING_BROKER", "EXITING"]}}
    ).to_list(500)
    signals_today = await db.signals.find(
        {"user_id": user_id, "created_at": {"$regex": f"^{date_str}"}}
    ).to_list(5000)
    daily_report = await db.daily_reports.find_one({"user_id": user_id, "date": date_str})

    return ProbeContext(
        db=db, user_id=user_id, date_str=date_str,
        strategies=strategies, closed_today=closed_today,
        open_positions=open_positions, signals_today=signals_today,
        daily_report=daily_report, now_ist_hm=hm, in_market_hours=in_hours,
    )


async def _persist(db, user_id: str, date_str: str,
                   findings: List[Finding], ran_probe_ids: List[str]) -> Dict[str, Any]:
    now_iso = datetime.now(timezone.utc).isoformat()
    emitted_keys: set = set()
    for f in findings:
        emitted_keys.add(f.key)
        doc = f.to_doc()
        doc.pop("narrative", None)  # narrative is written by the narrator, not here
        await db.hermes_findings.update_one(
            {"user_id": user_id, "key": f.key},
            {"$set": {**doc, "user_id": user_id, "status": "open",
                      "last_seen": now_iso, "last_seen_date": date_str},
             "$setOnInsert": {"first_seen": now_iso, "first_seen_date": date_str},
             "$inc": {"occurrences": 1}},
            upsert=True,
        )
    # Auto-verify-on-fix: an open finding whose probe RAN this cycle but did NOT
    # re-emit is considered resolved (the fix landed, or the condition cleared).
    ran = set(ran_probe_ids)
    resolved = 0
    async for prev in db.hermes_findings.find(
        {"user_id": user_id, "status": "open"}, {"key": 1, "probe_id": 1, "entity": 1}
    ):
        probe_succeeded = (
            prev.get("probe_id") in ran
            or (
                prev.get("probe_id") == "infra.probe_error"
                and prev.get("entity") in ran
            )
        )
        if probe_succeeded and prev.get("key") not in emitted_keys:
            await db.hermes_findings.update_one(
                {"user_id": user_id, "key": prev.get("key")},
                {"$set": {"status": "resolved", "resolved_at": now_iso,
                          "resolved_date": date_str}},
            )
            resolved += 1
    return {"emitted": len(emitted_keys), "resolved": resolved}


def _summarize(findings: List[Finding]) -> Dict[str, Any]:
    by_sev: Dict[str, int] = {}
    by_domain: Dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        by_domain[f.domain] = by_domain.get(f.domain, 0) + 1
    return {"total": len(findings), "by_severity": by_sev, "by_domain": by_domain}


async def run_diagnostics(db, user_id: str, date_str: Optional[str] = None,
                          kinds: Optional[List[str]] = None,
                          now: Optional[datetime] = None,
                          persist: bool = True,
                          auto_oos: bool = False) -> Dict[str, Any]:
    """Run the diagnostician for one user/day.

    kinds: subset of {"static","dynamic"} to run (default both). The intraday pass
    can run only "dynamic" (freshness/marks); EOD runs both.
    Returns {summary, findings (sorted, severe-first), persist_stats}.
    """
    now = now or datetime.now(timezone.utc)
    if date_str is None:
        date_str = now.astimezone(_IST).strftime("%Y-%m-%d")
    ctx = await _build_context(db, user_id, date_str, now)

    specs = [s for s in probe_sdk.all_probes()
             if kinds is None or s.kind in kinds]
    findings: List[Finding] = []
    ran_ids: List[str] = []
    for spec in specs:
        emitted = await run_probe_safe(spec, ctx)
        crashed = any(
            f.probe_id == "infra.probe_error" and f.entity == spec.probe_id
            for f in emitted
        )
        if not crashed:
            ran_ids.append(spec.probe_id)
        findings.extend(emitted)

    # Verifier (Layer 3): findings are deterministic; a probe that emitted with
    # real evidence is CONFIRMED. Probes already return nothing (not a guess) when
    # evidence is thin, so INSUFFICIENT_DATA is expressed as silence.
    for f in findings:
        if not f.confidence:
            f.confidence = Confidence.CONFIRMED

    findings.sort(key=lambda f: (Severity.rank(f.severity), f.domain))

    finding_docs = [f.to_doc() for f in findings]
    persist_stats: Dict[str, Any] = {}
    task_stats: Dict[str, Any] = {}
    if persist:
        persist_stats = await _persist(db, user_id, date_str, findings, ran_ids)
        # Handoff: auto-file high-severity findings as fix-tasks (findings persisted
        # first so auto-close can read their resolved status). Best-effort.
        try:
            from core.hermes_diagnostics.fix_tasks import sync_fix_tasks
            task_stats = await sync_fix_tasks(db, user_id, date_str, finding_docs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fix-task sync failed: %s", exc)
        # Research bridge: edge-doubt findings (persistent loss / bad geometry)
        # become research hypotheses so they get tested, not just noticed.
        try:
            from core.research_bridge import sync_findings_to_research
            task_stats["research_hypotheses"] = await sync_findings_to_research(
                db, user_id, finding_docs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("research bridge finding sync failed: %s", exc)
        # Auto-OOS: for edge-doubt findings, replay the strategy over real stored
        # data and attach the walk-forward verdict to its hypothesis — no manual
        # backtest trigger. Bounded + threaded; EOD-only (auto_oos=True) so the
        # manual audit button stays fast.
        if auto_oos:
            try:
                from core.hermes_diagnostics.auto_oos import run_auto_oos_for_findings
                task_stats["auto_oos"] = await run_auto_oos_for_findings(
                    db, user_id, finding_docs)
            except Exception as exc:  # noqa: BLE001
                logger.warning("auto-OOS pass failed: %s", exc)
        await db.hermes_diagnostic_runs.update_one(
            {"user_id": user_id, "date": date_str},
            {"$set": {"user_id": user_id, "date": date_str,
                      "ran_at": now.isoformat(), "ran_probes": len(ran_ids),
                      "summary": _summarize(findings),
                      "persist": persist_stats, "fix_tasks": task_stats}},
            upsert=True,
        )

    return {"date": date_str, "summary": _summarize(findings),
            "findings": finding_docs, "persist": persist_stats,
            "fix_tasks": task_stats}
