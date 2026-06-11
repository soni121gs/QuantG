from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_notification_once(
    db,
    *,
    user_id: str,
    type: str,
    severity: str,
    title: str,
    message: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    action_url: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    browser_alert: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Create one user notification, deduped by stable event key."""
    key = dedupe_key or f"{user_id}:{type}:{entity_type or 'none'}:{entity_id or title}"
    existing = await db.notifications.find_one({"user_id": user_id, "dedupe_key": key}, {"_id": 0})
    if existing:
        return None

    now = utc_now_iso()
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "type": type,
        "severity": severity,
        "title": title,
        "message": message,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "action_url": action_url,
        "read": False,
        "browser_alert": bool(browser_alert or severity == "critical"),
        "dedupe_key": key,
        "metadata": metadata or {},
        "created_at": now,
        "updated_at": now,
    }
    await db.notifications.insert_one(doc)
    doc.pop("_id", None)
    return doc


def _order_title(status: str) -> str:
    s = str(status or "").upper()
    if s in {"FILLED", "COMPLETE", "CLOSED"}:
        return "Order filled"
    if s in {"FAILED", "REJECTED", "BROKER_NOT_FOUND"}:
        return "Order needs attention"
    return "Order update"


async def generate_user_notifications(db, user_id: str) -> None:
    """Backfill recent actionable notifications from current trading state."""
    orders = await db.orders.find(
        {
            "user_id": user_id,
            "status": {
                "$in": [
                    "FAILED",
                    "REJECTED",
                    "BROKER_NOT_FOUND",
                    "UNKNOWN_NEEDS_REVIEW",
                    "FILLED",
                    "COMPLETE",
                    "CLOSED",
                ]
            },
        },
        {"_id": 0},
    ).sort("created_at", -1).to_list(80)

    for order in orders:
        status = str(order.get("status") or "").upper()
        oid = str(order.get("id") or order.get("order_id") or order.get("dedupe_key") or "")
        symbol = order.get("symbol") or order.get("trading_symbol") or "order"
        side = order.get("side") or order.get("transaction_type") or ""
        qty = order.get("qty") or order.get("quantity") or ""
        critical = status in {"FAILED", "REJECTED", "BROKER_NOT_FOUND", "UNKNOWN_NEEDS_REVIEW"}
        await create_notification_once(
            db,
            user_id=user_id,
            type="order_failed" if critical else "order_filled",
            severity="critical" if critical else "success",
            title=_order_title(status),
            message=f"{symbol} {side} {qty} is {status}.",
            entity_type="order",
            entity_id=oid or None,
            action_url="/orders",
            dedupe_key=f"order:{user_id}:{oid or symbol}:{status}",
            browser_alert=True,
            metadata={"status": status, "symbol": symbol},
        )

    halted = await db.strategies.find(
        {
            "user_id": user_id,
            "$or": [
                {"halted": True},
                {"is_halted": True},
                {"last_error": {"$exists": True, "$ne": ""}},
            ],
        },
        {"_id": 0, "id": 1, "name": 1, "last_error": 1, "halt_reason": 1},
    ).limit(60).to_list(60)
    for strategy in halted:
        sid = str(strategy.get("id") or "")
        reason = strategy.get("halt_reason") or strategy.get("last_error") or "Strategy needs review."
        await create_notification_once(
            db,
            user_id=user_id,
            type="strategy",
            severity="warning",
            title="Strategy needs review",
            message=f"{strategy.get('name') or 'Strategy'}: {str(reason)[:160]}",
            entity_type="strategy",
            entity_id=sid or None,
            action_url="/strategies",
            dedupe_key=f"strategy-halt:{user_id}:{sid}:{str(reason)[:80]}",
            browser_alert=False,
        )

    risk_state = await db.risk_state.find_one({"_id": f"position_reconciliation:{user_id}"}, {"_id": 0})
    if risk_state and risk_state.get("mismatch"):
        await create_notification_once(
            db,
            user_id=user_id,
            type="risk",
            severity="critical",
            title="Broker reconciliation mismatch",
            message="QuantG detected a mismatch between local and broker position state.",
            entity_type="risk",
            entity_id=f"position_reconciliation:{user_id}",
            action_url="/ops",
            dedupe_key=f"reconciliation-mismatch:{user_id}",
            browser_alert=True,
        )
