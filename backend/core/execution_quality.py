"""Execution-quality telemetry for paper/live fills.

This is an audit layer, not an execution path. It writes best-effort records to
db.execution_quality so research and promotion gates can compare expected prices,
actual fills, slippage, charges, and fill realism without changing order routing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import uuid


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _i(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def build_quality_doc(
    *,
    order: Dict[str, Any],
    fill: Optional[Dict[str, Any]] = None,
    position: Optional[Dict[str, Any]] = None,
    event: str = "fill",
    expected_price: Optional[float] = None,
    actual_price: Optional[float] = None,
    quantity: Optional[int] = None,
    status: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize one order/fill into an execution-quality record."""

    fill = fill or {}
    position = position or {}
    now = datetime.now(timezone.utc)
    side = str(order.get("side") or fill.get("side") or "").upper()
    expected = _f(
        expected_price
        if expected_price is not None
        else order.get("expected_price")
        or order.get("requested_price")
        or fill.get("expected_price")
    )
    actual = _f(
        actual_price
        if actual_price is not None
        else fill.get("price")
        or fill.get("fill_price")
        or order.get("price")
    )
    qty = _i(quantity if quantity is not None else fill.get("qty") or order.get("filled_qty") or order.get("qty"))
    charges = _f(fill.get("charges") or order.get("charges") or fill.get("brokerage") or order.get("brokerage"))
    modeled_slippage_per_unit = _f(fill.get("slippage") or order.get("slippage"))
    if expected > 0 and actual > 0:
        signed_slip = (actual - expected) if side == "BUY" else (expected - actual)
        adverse_slippage = max(0.0, signed_slip)
    else:
        signed_slip = 0.0
        adverse_slippage = modeled_slippage_per_unit
    slippage_amount = round(adverse_slippage * max(qty, 0), 2)
    notional = abs(actual * qty)
    created_at = _parse_dt(order.get("created_at"))
    filled_at = _parse_dt(fill.get("created_at") or fill.get("filled_at") or order.get("updated_at"))
    fill_delay_ms = None
    if created_at and filled_at:
        fill_delay_ms = max(0, int((filled_at - created_at).total_seconds() * 1000))

    order_id = str(order.get("id") or fill.get("order_id") or f"unknown_{uuid.uuid4().hex[:8]}")
    leg_role = order.get("spread_role") or fill.get("spread_role")
    return {
        "id": f"eq_{uuid.uuid4().hex[:12]}",
        "dedupe_key": f"{order_id}:{event}:{leg_role or ''}",
        "event": event,
        "user_id": order.get("user_id") or fill.get("user_id") or position.get("user_id"),
        "strategy_id": order.get("strategy_id") or fill.get("strategy_id") or position.get("strategy_id"),
        "position_id": order.get("position_id") or fill.get("position_id") or position.get("id"),
        "order_id": order_id,
        "broker_order_id": order.get("broker_order_id") or fill.get("broker_order_id"),
        "symbol": order.get("symbol") or fill.get("symbol") or position.get("symbol"),
        "target_symbol": order.get("target_symbol") or fill.get("target_symbol") or position.get("target_symbol"),
        "instrument_key": order.get("instrument_key") or fill.get("instrument_key"),
        "mode": order.get("mode") or fill.get("mode") or position.get("mode"),
        "broker": order.get("broker") or fill.get("broker"),
        "structure": order.get("structure") or fill.get("structure") or position.get("structure"),
        "spread_role": leg_role,
        "side": side,
        "qty": qty,
        "expected_price": round(expected, 4),
        "actual_price": round(actual, 4),
        "signed_slippage_per_unit": round(signed_slip, 4),
        "adverse_slippage_per_unit": round(adverse_slippage, 4),
        "modeled_slippage_per_unit": round(modeled_slippage_per_unit, 4),
        "slippage_amount": slippage_amount,
        "charges": round(charges, 2),
        "cost_amount": round(slippage_amount + charges, 2),
        "cost_bps": round(((slippage_amount + charges) / notional) * 10000, 2) if notional > 0 else None,
        "fill_delay_ms": fill_delay_ms,
        "fill_status": status or order.get("execution_status") or order.get("status"),
        "missed_fill_reason": reason if status in {"REJECTED", "FAILED", "SKIPPED"} else None,
        "quality_grade": quality_grade(slippage_amount, charges, notional, status or order.get("execution_status") or order.get("status")),
        "created_at": now.isoformat(),
        "order_created_at": _iso(order.get("created_at")),
        "filled_at": _iso(fill.get("created_at") or fill.get("filled_at") or order.get("updated_at")),
    }


def quality_grade(slippage_amount: float, charges: float, notional: float, status: Any) -> str:
    status_s = str(status or "").upper()
    if status_s in {"REJECTED", "FAILED", "SKIPPED", "SKIPPED_SIGNAL"}:
        return "MISSED"
    if notional <= 0:
        return "UNKNOWN"
    bps = ((slippage_amount + charges) / notional) * 10000
    if bps <= 8:
        return "GOOD"
    if bps <= 25:
        return "OK"
    return "EXPENSIVE"


async def record_execution_quality(
    db: Any,
    *,
    order: Dict[str, Any],
    fill: Optional[Dict[str, Any]] = None,
    position: Optional[Dict[str, Any]] = None,
    event: str = "fill",
    expected_price: Optional[float] = None,
    actual_price: Optional[float] = None,
    quantity: Optional[int] = None,
    status: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Best-effort write. A telemetry failure must never break execution."""

    doc = build_quality_doc(
        order=order,
        fill=fill,
        position=position,
        event=event,
        expected_price=expected_price,
        actual_price=actual_price,
        quantity=quantity,
        status=status,
        reason=reason,
    )
    try:
        await db.execution_quality.update_one(
            {"dedupe_key": doc["dedupe_key"]},
            {"$setOnInsert": doc},
            upsert=True,
        )
    except Exception:
        return {"ok": False, "reason": "execution-quality-write-failed", "doc": doc}
    return {"ok": True, "doc": doc}


async def execution_quality_report(db: Any, user_id: str, *, days: int = 30) -> Dict[str, Any]:
    days = max(1, min(int(days or 30), 180))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since.isoformat()
    rows = await db.execution_quality.find(
        {"user_id": user_id, "created_at": {"$gte": since_iso}},
        {"_id": 0},
    ).sort("created_at", -1).to_list(5000)

    buckets: Dict[str, Dict[str, Any]] = {}
    total_cost = 0.0
    total_slip = 0.0
    total_charges = 0.0
    expensive = 0
    missed = 0
    for row in rows:
        total_cost += _f(row.get("cost_amount"))
        total_slip += _f(row.get("slippage_amount"))
        total_charges += _f(row.get("charges"))
        if row.get("quality_grade") == "EXPENSIVE":
            expensive += 1
        if row.get("quality_grade") == "MISSED":
            missed += 1
        key = str(row.get("strategy_id") or "UNKNOWN")
        b = buckets.setdefault(key, {
            "strategy_id": key,
            "fills": 0,
            "cost_amount": 0.0,
            "slippage_amount": 0.0,
            "charges": 0.0,
            "expensive_fills": 0,
            "missed_fills": 0,
        })
        b["fills"] += 1
        b["cost_amount"] += _f(row.get("cost_amount"))
        b["slippage_amount"] += _f(row.get("slippage_amount"))
        b["charges"] += _f(row.get("charges"))
        if row.get("quality_grade") == "EXPENSIVE":
            b["expensive_fills"] += 1
        if row.get("quality_grade") == "MISSED":
            b["missed_fills"] += 1

    by_strategy = []
    for b in buckets.values():
        b["cost_amount"] = round(b["cost_amount"], 2)
        b["slippage_amount"] = round(b["slippage_amount"], 2)
        b["charges"] = round(b["charges"], 2)
        b["avg_cost_per_fill"] = round(b["cost_amount"] / max(1, b["fills"]), 2)
        by_strategy.append(b)
    by_strategy.sort(key=lambda r: r["cost_amount"], reverse=True)

    return {
        "kind": "execution_quality_report",
        "days": days,
        "since": since_iso,
        "summary": {
            "fills": len(rows),
            "total_cost_amount": round(total_cost, 2),
            "slippage_amount": round(total_slip, 2),
            "charges": round(total_charges, 2),
            "expensive_fills": expensive,
            "missed_fills": missed,
        },
        "by_strategy": by_strategy[:50],
        "recent": rows[:50],
        "note": "Read-only telemetry. It records execution cost evidence; it does not change order routing or strategy status.",
    }
