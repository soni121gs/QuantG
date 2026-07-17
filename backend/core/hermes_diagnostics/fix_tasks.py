"""Auto-filed fix-tasks (handoff Layer).

Promotes CONFIRMED findings at/above a severity threshold into an actionable
`db.hermes_fix_tasks` queue — self-contained tickets an agent (or the founder)
can pick up without re-deriving context. This is the "flip" from review-first to
auto-filing: the diagnostician still NEVER changes code; it just files the ticket.

Lifecycle (founder-controlled, except auto-close):
    open ──(founder)──> in_progress ──(founder)──> done
      └──────────── auto_closed (the underlying finding resolved) ◄─ fix landed

A task is keyed by the finding `key` (probe::entity), so a recurring finding
updates its one ticket instead of spawning duplicates. When the finding's probe
stops emitting (a fix landed, verified by the runner's auto-resolve), the open
ticket is auto-closed — closing the loop end to end.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from core.hermes_diagnostics.contract import Severity

logger = logging.getLogger("quantg.hermes_diagnostics")

# Default ON — this module only files tickets, never touches trading or code.
_ENABLED = os.environ.get("HERMES_AUTOFILE_FIX_TASKS", "true").lower() == "true"
# Minimum severity that becomes a ticket (critical|high|medium|low).
_MIN_SEVERITY = os.environ.get("HERMES_FIX_TASK_MIN_SEVERITY", "high").lower()
# Statuses a founder can set; auto-close is system-only.
OPEN_STATUSES = ("open", "in_progress")


def _passes_threshold(sev: str) -> bool:
    return Severity.rank(sev) <= Severity.rank(_MIN_SEVERITY)


async def sync_fix_tasks(db, user_id: str, date_str: str,
                         findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Upsert fix-tasks for high-severity findings; auto-close tasks whose finding
    has resolved. Returns {filed, auto_closed, enabled}."""
    if not _ENABLED:
        return {"enabled": False, "filed": 0, "auto_closed": 0}

    now_iso = datetime.now(timezone.utc).isoformat()
    filed = 0
    filed_keys: set = set()
    for f in findings:
        if f.get("confidence", "confirmed") != "confirmed":
            continue
        if not _passes_threshold(f.get("severity", "low")):
            continue
        key = f.get("key")
        if not key:
            continue
        filed_keys.add(key)
        await db.hermes_fix_tasks.update_one(
            {"user_id": user_id, "key": key},
            {"$set": {
                "user_id": user_id, "key": key, "probe_id": f.get("probe_id"),
                "domain": f.get("domain"), "severity": f.get("severity"),
                "entity": f.get("entity"), "title": f.get("title"),
                "detail": f.get("detail"), "evidence": f.get("evidence"),
                "reproduction": f.get("reproduction"),
                "suggested_fix": f.get("suggested_fix"),
                "last_seen": now_iso, "last_seen_date": date_str,
             },
             "$setOnInsert": {"status": "open", "created_at": now_iso,
                              "filed_from_date": date_str},
             "$inc": {"occurrences": 1}},
            upsert=True,
        )
        filed += 1

    # Auto-close tickets whose underlying finding has resolved. A finding is
    # resolved when its hermes_findings doc is status=resolved (or absent).
    auto_closed = 0
    async for task in db.hermes_fix_tasks.find(
        {"user_id": user_id, "status": {"$in": list(OPEN_STATUSES)}},
        {"key": 1},
    ):
        key = task.get("key")
        finding = await db.hermes_findings.find_one(
            {"user_id": user_id, "key": key}, {"status": 1})
        if not finding or finding.get("status") == "resolved":
            await db.hermes_fix_tasks.update_one(
                {"user_id": user_id, "key": key},
                {"$set": {"status": "auto_closed", "closed_at": now_iso,
                          "closed_reason": "underlying finding resolved"}},
            )
            auto_closed += 1

    if filed or auto_closed:
        logger.info("Hermes fix-tasks user=%s filed=%d auto_closed=%d (min_sev=%s)",
                    user_id, filed, auto_closed, _MIN_SEVERITY)
    return {"enabled": True, "filed": filed, "auto_closed": auto_closed}
