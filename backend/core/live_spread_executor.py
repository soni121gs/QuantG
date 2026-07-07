"""Live (real-money) credit-spread leg execution via Upstox.

This module owns ONLY broker leg execution for spreads: place a leg order,
await its fill, and enforce the never-naked-short invariant. All position
accounting (docs, trades, trade_fills, today_pnl) stays in
core/spread_lifecycle.py, which calls into this module when a spread's mode
is "live" — paper and live share one accounting path, only the fill source
differs (the §11.8 execution-port invariant).

Safety gates (ALL must pass or the entry is rejected, fail-closed):
  1. CORE_ENGINE_LIVE_ENABLED=true          (env — founder-controlled)
  2. LIVE_SPREADS_ENABLED=true              (env — spreads-specific arm, default false)
  3. live_arm_state.armed=true              (enforced inside the injected place fn)
  4. both legs carry real Upstox pipe instrument keys
  5. executor configured (server.py injects the place/details callables at startup)

Leg ordering invariants:
  ENTRY: BUY the protective long leg FIRST, then SELL the short leg. If the
         short leg fails, the long leg is immediately unwound (best-effort;
         a failed unwind is recorded to db.live_spread_incidents and logged).
         The book can transiently be long-only, NEVER naked-short.
  EXIT:  BUY back the short leg FIRST, then SELL the long leg. Progress is
         persisted per-leg on the position doc (live_close_state), so a retry
         after a partial close never re-buys the short.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger("quantg.live_spread_executor")

FILL_POLL_SECONDS = float(os.environ.get("LIVE_SPREAD_FILL_POLL_SECONDS", "1.0"))
FILL_TIMEOUT_SECONDS = float(os.environ.get("LIVE_SPREAD_FILL_TIMEOUT_SECONDS", "25"))

_FILLED_STATUSES = {"complete", "completed", "filled", "full_fill"}
_DEAD_STATUSES = {"rejected", "cancelled", "canceled", "lapsed", "expired"}

# Injected by server.py at startup (configure()). Both are async callables.
_place_order_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None
_order_details_fn: Optional[Callable[[str, str], Awaitable[Dict[str, Any]]]] = None


def configure(
    *,
    place_order_fn: Callable[..., Awaitable[Dict[str, Any]]],
    order_details_fn: Callable[[str, str], Awaitable[Dict[str, Any]]],
) -> None:
    """server.py injects its Upstox order primitives here at startup."""
    global _place_order_fn, _order_details_fn
    _place_order_fn = place_order_fn
    _order_details_fn = order_details_fn


def live_spreads_enabled() -> bool:
    return (
        os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true"
        and os.environ.get("LIVE_SPREADS_ENABLED", "false").lower() == "true"
    )


def _gate_reason() -> Optional[str]:
    if os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() != "true":
        return "live spreads blocked: CORE_ENGINE_LIVE_ENABLED is not true"
    if os.environ.get("LIVE_SPREADS_ENABLED", "false").lower() != "true":
        return "live spreads blocked: LIVE_SPREADS_ENABLED is not true"
    if _place_order_fn is None or _order_details_fn is None:
        return "live spreads blocked: executor not configured (server startup wiring missing)"
    return None


def _valid_key(key: Any) -> bool:
    k = str(key or "").strip()
    return bool(k) and "|" in k and not k.upper().startswith("PAPER_") and not k.lower().startswith("mock_")


def _details_status(payload: Dict[str, Any]) -> str:
    data = payload.get("data") if isinstance(payload, dict) else None
    row = data if isinstance(data, dict) else (payload if isinstance(payload, dict) else {})
    return str(row.get("status") or row.get("order_status") or "").strip().lower()


def _details_avg_price(payload: Dict[str, Any]) -> float:
    data = payload.get("data") if isinstance(payload, dict) else None
    row = data if isinstance(data, dict) else (payload if isinstance(payload, dict) else {})
    try:
        return float(row.get("average_price") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


async def _await_fill(user_id: str, broker_order_id: str) -> Dict[str, Any]:
    """Poll order details until the order completes. Fail-closed on timeout."""
    deadline = asyncio.get_event_loop().time() + FILL_TIMEOUT_SECONDS
    last_status = ""
    while asyncio.get_event_loop().time() < deadline:
        try:
            payload = await _order_details_fn(user_id, broker_order_id)
            last_status = _details_status(payload)
            if last_status in _FILLED_STATUSES:
                avg = _details_avg_price(payload)
                if avg > 0:
                    return {"ok": True, "avg_price": avg, "status": last_status}
                # complete but no price yet — poll once more
            elif last_status in _DEAD_STATUSES:
                return {"ok": False, "error": f"order {last_status}", "status": last_status}
        except Exception as exc:  # noqa: BLE001
            logger.warning("live spread fill poll failed order=%s: %s", broker_order_id, exc)
        await asyncio.sleep(FILL_POLL_SECONDS)
    return {"ok": False, "error": f"fill confirmation timeout ({last_status or 'no status'})",
            "status": last_status or "unknown", "timeout": True}


async def _execute_leg(
    user_id: str,
    *,
    instrument_key: str,
    side: str,
    qty: int,
    tag: str,
) -> Dict[str, Any]:
    """Place one MARKET leg and await its fill. Returns {ok, avg_price, broker_order_id}."""
    try:
        placed = await _place_order_fn(
            user_id,
            instrument_token=instrument_key,
            side=side,
            quantity=int(qty),
            order_type="MARKET",
            product="D",   # NRML — spreads may ride to 15:25 / expiry
            tag=tag[:40],
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"order placement failed: {exc}"}
    broker_order_id = str(placed.get("broker_order_id") or placed.get("order_id") or "")
    if not broker_order_id:
        return {"ok": False, "error": "broker did not return an order id", "raw": placed}
    fill = await _await_fill(user_id, broker_order_id)
    fill["broker_order_id"] = broker_order_id
    return fill


async def _record_incident(db, user_id: str, kind: str, detail: Dict[str, Any]) -> None:
    try:
        await db.live_spread_incidents.insert_one({
            "id": f"lsi_{uuid.uuid4().hex[:12]}",
            "user_id": user_id,
            "kind": kind,
            "detail": detail,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("live spread incident write failed (%s): %s | %s", kind, exc, detail)


async def execute_spread_entry(
    db,
    *,
    user_id: str,
    spread: Dict[str, Any],
    qty: int,
    tag_prefix: str,
) -> Dict[str, Any]:
    """BUY long wing, then SELL short leg. Never leaves a naked short.

    Returns {ok, short_fill, long_fill, entry_order_ids} on success;
    {ok: False, stage, error} on failure (long leg unwound best-effort).
    """
    reason = _gate_reason()
    if reason:
        return {"ok": False, "stage": "gate", "error": reason}
    short = spread.get("short_leg") or {}
    long = spread.get("long_leg") or {}
    if not _valid_key(short.get("instrument_key")) or not _valid_key(long.get("instrument_key")):
        return {"ok": False, "stage": "gate",
                "error": "live spreads blocked: leg instrument_key is not a real Upstox pipe key"}

    r_long = await _execute_leg(
        user_id, instrument_key=str(long["instrument_key"]), side="BUY", qty=qty,
        tag=f"{tag_prefix}:Lo",
    )
    if not r_long.get("ok"):
        logger.warning("live spread entry: long leg failed user=%s: %s", user_id, r_long.get("error"))
        return {"ok": False, "stage": "long_entry", "error": r_long.get("error")}

    r_short = await _execute_leg(
        user_id, instrument_key=str(short["instrument_key"]), side="SELL", qty=qty,
        tag=f"{tag_prefix}:Sh",
    )
    if not r_short.get("ok"):
        # Unwind the long wing immediately so the entry is atomic-or-nothing.
        r_unwind = await _execute_leg(
            user_id, instrument_key=str(long["instrument_key"]), side="SELL", qty=qty,
            tag=f"{tag_prefix}:Uw",
        )
        if not r_unwind.get("ok"):
            await _record_incident(db, user_id, "ORPHAN_LONG_LEG", {
                "instrument_key": long.get("instrument_key"), "qty": qty,
                "long_order_id": r_long.get("broker_order_id"),
                "unwind_error": r_unwind.get("error"),
                "short_error": r_short.get("error"),
            })
            logger.error(
                "live spread entry: SHORT leg failed AND long unwind failed — ORPHAN long %s x%s "
                "at broker. Manual/reconciler action required.",
                long.get("instrument_key"), qty,
            )
        return {"ok": False, "stage": "short_entry", "error": r_short.get("error"),
                "unwound": bool(r_unwind.get("ok"))}

    return {
        "ok": True,
        "short_fill": float(r_short["avg_price"]),
        "long_fill": float(r_long["avg_price"]),
        "entry_order_ids": {
            "long": r_long.get("broker_order_id"),
            "short": r_short.get("broker_order_id"),
        },
    }


async def unwind_spread_legs(db, position: Dict[str, Any], *, reason: str) -> Dict[str, Any]:
    """Close a live spread: BUY back the short leg, then SELL the long leg.

    Per-leg progress is persisted on the position doc (live_close_state), so a
    retry after a partial close skips already-filled legs — the short can never
    be bought back twice. Returns {ok, short_fill, long_fill} on success.
    """
    if _place_order_fn is None or _order_details_fn is None:
        return {"ok": False, "stage": "gate",
                "error": "live spreads blocked: executor not configured"}
    pos_id = position["id"]
    user_id = position["user_id"]
    qty = int(position.get("open_quantity") or position.get("quantity") or 0)
    legs = position.get("legs") or []
    short = next((l for l in legs if l.get("role") == "short"), None)
    long = next((l for l in legs if l.get("role") == "long"), None)
    if not short or not long or qty <= 0:
        return {"ok": False, "stage": "gate", "error": "live spread close: missing legs or qty"}

    state = dict(position.get("live_close_state") or {})

    if not state.get("short_closed"):
        r_short = await _execute_leg(
            user_id, instrument_key=str(short.get("instrument_key")), side="BUY", qty=qty,
            tag=f"x:{pos_id[:14]}:Sh",
        )
        if not r_short.get("ok"):
            return {"ok": False, "stage": "short_close", "error": r_short.get("error")}
        state.update({"short_closed": True, "short_fill": float(r_short["avg_price"]),
                      "close_short_order_id": r_short.get("broker_order_id")})
        await db.strategy_positions.update_one(
            {"id": pos_id, "user_id": user_id},
            {"$set": {"live_close_state": state,
                      "updated_at": datetime.now(timezone.utc).isoformat()}},
        )

    if not state.get("long_closed"):
        r_long = await _execute_leg(
            user_id, instrument_key=str(long.get("instrument_key")), side="SELL", qty=qty,
            tag=f"x:{pos_id[:14]}:Lo",
        )
        if not r_long.get("ok"):
            # Short is bought back — remaining exposure is long-only (benign).
            # State is persisted; the monitor's retry will skip the short leg.
            await _record_incident(db, user_id, "PARTIAL_SPREAD_CLOSE", {
                "position_id": pos_id, "reason": reason,
                "long_error": r_long.get("error"),
                "state": state,
            })
            return {"ok": False, "stage": "long_close", "error": r_long.get("error"),
                    "partial": True}
        state.update({"long_closed": True, "long_fill": float(r_long["avg_price"]),
                      "close_long_order_id": r_long.get("broker_order_id")})
        await db.strategy_positions.update_one(
            {"id": pos_id, "user_id": user_id},
            {"$set": {"live_close_state": state,
                      "updated_at": datetime.now(timezone.utc).isoformat()}},
        )

    return {"ok": True,
            "short_fill": float(state.get("short_fill") or 0.0),
            "long_fill": float(state.get("long_fill") or 0.0),
            "close_order_ids": {"short": state.get("close_short_order_id"),
                                "long": state.get("close_long_order_id")}}
