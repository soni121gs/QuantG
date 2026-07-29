"""Strategy status audit trail.

Every pause/resume of a strategy — manual (founder clicking the card), bulk
(ops enable-all / emergency-stop / kill-switch) or automatic (the 09:15 IST
scheduler wake, the 15:30 sleep) — writes one row here.

Why this exists: before 2026-07-29 a status change left NO trace at all. The
toggle route did not stamp `updated_at`, there was no audit collection, and the
only way to reconstruct "was this strategy running at 12:45?" was to look for
gaps in the 5-minute signal cadence. On 2026-07-29 the founder paused and
resumed strategies mid-session and afterwards there was no way to tell which
rows were touched, when, or whether the resume was what unblocked trading.

Append-only, best-effort: an audit failure must NEVER block a status change.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

COLLECTION = "strategy_status_audit"

# Who/what flipped the switch. `manual` is the founder in the UI; everything
# else is the system acting on its own schedule or a risk rule firing.
ACTOR_MANUAL = "manual"
ACTOR_SCHEDULER = "scheduler"
ACTOR_RISK = "risk"
ACTOR_OPS = "ops"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ist_clock(iso: str) -> str:
    """15:30 IST reads better than 10:00Z when reconstructing a session."""
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ist = dt.astimezone(timezone.utc).timestamp() + (5 * 3600 + 1800)
        return datetime.fromtimestamp(ist, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S IST")
    except Exception:
        return iso


async def record_status_change(
    db: Any,
    *,
    user_id: str,
    strategy_id: str,
    name: str | None = None,
    old_status: str | None = None,
    new_status: str | None = None,
    actor: str = ACTOR_MANUAL,
    source: str = "",
    detail: Mapping[str, Any] | None = None,
) -> None:
    """Append one status-change row. Never raises."""
    if old_status == new_status:
        return
    at = _now()
    try:
        await db[COLLECTION].insert_one({
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "strategy_id": strategy_id,
            "strategy_name": name,
            "old_status": old_status,
            "new_status": new_status,
            "actor": actor,
            "source": source,
            "detail": dict(detail or {}),
            "at": at,
            "at_ist": _ist_clock(at),
        })
    except Exception as exc:  # pragma: no cover - audit must never break trading
        logger.warning("strategy_status_audit write failed for %s: %s", strategy_id, exc)


async def record_bulk_status_change(
    db: Any,
    *,
    user_id: str,
    rows: Iterable[Mapping[str, Any]],
    new_status: str,
    actor: str,
    source: str,
    detail: Mapping[str, Any] | None = None,
) -> int:
    """Audit a bulk flip. `rows` are the strategy docs as they were BEFORE the
    update — call this before or alongside the update_many, not after, or the
    old status is already lost."""
    written = 0
    for row in rows or []:
        old = row.get("status")
        if old == new_status:
            continue
        await record_status_change(
            db,
            user_id=user_id,
            strategy_id=row.get("id") or row.get("strategy_id") or "",
            name=row.get("name"),
            old_status=old,
            new_status=new_status,
            actor=actor,
            source=source,
            detail=detail,
        )
        written += 1
    return written
