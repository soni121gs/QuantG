"""Canonical QuantG order lifecycle helpers.

The broker APIs all expose slightly different status names. Internally we keep
the production lifecycle small and explicit, then preserve raw broker status in
``broker_status`` for audit/debugging.
"""
from __future__ import annotations

from typing import Any, Optional


ORDER_NEW = "NEW"
ORDER_PLACED = "PLACED"
ORDER_OPEN = "OPEN"
ORDER_PARTIAL_FILL = "PARTIAL_FILL"
ORDER_FILLED = "FILLED"
ORDER_CANCELLED = "CANCELLED"
ORDER_REJECTED = "REJECTED"
ORDER_EXIT_PENDING = "EXIT_PENDING"
ORDER_CLOSED = "CLOSED"

ORDER_TERMINAL_STATUSES = {
    ORDER_FILLED,
    ORDER_CANCELLED,
    ORDER_REJECTED,
    ORDER_CLOSED,
}

ORDER_ACTIVE_STATUSES = {
    ORDER_NEW,
    ORDER_PLACED,
    ORDER_OPEN,
    ORDER_PARTIAL_FILL,
    ORDER_EXIT_PENDING,
}

LEGACY_OPEN_STATUSES = {
    "PENDING",
    "PENDING_BROKER",
    "TRIGGER PENDING",
    "MODIFY PENDING",
    "VALIDATION PENDING",
    "PUT ORDER REQ RECEIVED",
}

LEGACY_FILLED_STATUSES = {
    "COMPLETE",
    "COMPLETED",
    "TRADED",
    "TRD",
}

LEGACY_TERMINAL_STATUSES = LEGACY_FILLED_STATUSES | {
    "CANCELLED",
    "CANCELED",
    "REJECTED",
    "STALE",
    "BROKER_NOT_FOUND",
}


def canonical_order_status(
    raw_status: Any,
    *,
    filled_qty: Optional[int] = None,
    pending_qty: Optional[int] = None,
) -> str:
    text = str(raw_status or "").strip().upper()
    if text in {ORDER_NEW, ORDER_PLACED, ORDER_OPEN, ORDER_PARTIAL_FILL, ORDER_FILLED, ORDER_CANCELLED, ORDER_REJECTED, ORDER_EXIT_PENDING, ORDER_CLOSED}:
        return text
    if text in LEGACY_FILLED_STATUSES:
        return ORDER_FILLED
    if text in {"CANCELLED", "CANCELED", "CXL"}:
        return ORDER_CANCELLED
    if text in {"REJECTED", "REJ", "FAILED"}:
        return ORDER_REJECTED
    try:
        filled = int(filled_qty or 0)
        pending = int(pending_qty or 0)
    except Exception:
        filled = 0
        pending = 0
    if filled > 0 and pending > 0:
        return ORDER_PARTIAL_FILL
    if text in LEGACY_OPEN_STATUSES or text == "OPEN":
        return ORDER_OPEN
    if text:
        return ORDER_OPEN
    return ORDER_NEW


def is_order_active(status: Any) -> bool:
    return canonical_order_status(status) in ORDER_ACTIVE_STATUSES


def is_order_terminal(status: Any) -> bool:
    return canonical_order_status(status) in ORDER_TERMINAL_STATUSES

