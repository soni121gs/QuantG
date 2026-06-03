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
ORDER_UNKNOWN_NEEDS_REVIEW = "UNKNOWN_NEEDS_REVIEW"
ORDER_PAPER_CREATED = "PAPER_ORDER_CREATED"
ORDER_PAPER_FILLED = "PAPER_FILLED"
ORDER_SKIPPED_SIGNAL = "SKIPPED_SIGNAL"

ORDER_TERMINAL_STATUSES = {
    ORDER_FILLED,
    ORDER_PAPER_FILLED,
    ORDER_CANCELLED,
    ORDER_REJECTED,
    ORDER_CLOSED,
    ORDER_UNKNOWN_NEEDS_REVIEW,
    ORDER_SKIPPED_SIGNAL,
}

ORDER_ACTIVE_STATUSES = {
    ORDER_NEW,
    ORDER_PLACED,
    ORDER_OPEN,
    ORDER_PARTIAL_FILL,
    ORDER_EXIT_PENDING,
    ORDER_PAPER_CREATED,
}

ALLOWED_ORDER_TRANSITIONS = {
    ORDER_NEW: {
        ORDER_PLACED,
        ORDER_OPEN,
        ORDER_REJECTED,
        ORDER_CANCELLED,
        ORDER_UNKNOWN_NEEDS_REVIEW,
    },
    ORDER_PLACED: {
        ORDER_OPEN,
        ORDER_PARTIAL_FILL,
        ORDER_FILLED,
        ORDER_REJECTED,
        ORDER_CANCELLED,
        ORDER_UNKNOWN_NEEDS_REVIEW,
    },
    ORDER_OPEN: {
        ORDER_PARTIAL_FILL,
        ORDER_FILLED,
        ORDER_CANCELLED,
        ORDER_REJECTED,
        ORDER_EXIT_PENDING,
        ORDER_UNKNOWN_NEEDS_REVIEW,
    },
    ORDER_PARTIAL_FILL: {
        ORDER_PARTIAL_FILL,
        ORDER_FILLED,
        ORDER_CANCELLED,
        ORDER_EXIT_PENDING,
        ORDER_UNKNOWN_NEEDS_REVIEW,
    },
    ORDER_EXIT_PENDING: {
        ORDER_CLOSED,
        ORDER_FILLED,
        ORDER_CANCELLED,
        ORDER_REJECTED,
        ORDER_UNKNOWN_NEEDS_REVIEW,
    },
    ORDER_PAPER_CREATED: {
        ORDER_PAPER_FILLED,
        ORDER_CANCELLED,
        ORDER_REJECTED,
        ORDER_SKIPPED_SIGNAL,
    },
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
    if text == ORDER_PAPER_FILLED:
        return ORDER_FILLED
    if text == ORDER_PAPER_CREATED:
        return ORDER_OPEN
    if text == ORDER_SKIPPED_SIGNAL:
        return ORDER_REJECTED
    if text in {ORDER_NEW, ORDER_PLACED, ORDER_OPEN, ORDER_PARTIAL_FILL, ORDER_FILLED, ORDER_CANCELLED, ORDER_REJECTED, ORDER_EXIT_PENDING, ORDER_CLOSED, ORDER_UNKNOWN_NEEDS_REVIEW}:
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


def validate_order_transition(current_status: Any, next_status: Any) -> str:
    """Return canonical next status or raise when the transition is unsafe."""
    current = canonical_order_status(current_status)
    next_canonical = canonical_order_status(next_status)
    if current == next_canonical:
        return next_canonical
    if current in ORDER_TERMINAL_STATUSES:
        raise ValueError(f"order state is terminal: {current} -> {next_canonical}")
    allowed = ALLOWED_ORDER_TRANSITIONS.get(current, set())
    if next_canonical not in allowed:
        raise ValueError(f"invalid order transition: {current} -> {next_canonical}")
    return next_canonical
