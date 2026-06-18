"""PositionGuardian — independent last-resort position closer.

Runs its own asyncio loop every GUARDIAN_POLL_SECONDS (default 5).
It is INDEPENDENT of the strategy loop, signal manager, and strategy_runner.
If every other loop dies, the guardian still closes positions.

Responsibilities:
  1. Verify every OPEN position has SL + TP + deadline. Assign defaults if missing.
  2. Check SL, TP, trailing-SL, and deadline using the LTP fallback chain.
  3. Fire close_fn on any exit condition — same idempotency key as position_monitor
     so duplicate orders are physically impossible.
  4. Force-close ALL positions at SQUAREOFF_MINUTE_IST (15:10).
  5. Force MARKET exit when LTP is completely unavailable AND deadline has passed.
  6. Log an alert (ERROR) if any position has been OPEN > MAX_UNMONITORED_SECONDS
     without an LTP update (possible zombie position).

Design constraints:
  - No imports from server.py (injected via run_guardian_loop).
  - Every position processes inside try/except — one crash never stops others.
  - Uses the same EXITING atomic-mark pattern as _close_strategy_positions to
    avoid racing with position_monitor.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Dict, Optional

from ltp_resolver import resolve_position_ltp
from core.position_lifecycle import (
    exit_reason,
    normalize_strategy_risk,
    position_risk_prices,
    parse_iso_dt,
)

logger = logging.getLogger("quantg.position_guardian")

# ── Config (all overridable via env) ──────────────────────────────────────────

GUARDIAN_POLL_SECONDS       = int(os.environ.get("GUARDIAN_POLL_SECONDS", "5"))
SQUAREOFF_MINUTE_IST        = int(os.environ.get("GUARDIAN_SQUAREOFF_MINUTE", str(15 * 60 + 10)))
MAX_UNMONITORED_SECONDS     = int(os.environ.get("GUARDIAN_MAX_UNMONITORED_SEC", "120"))
# Widened to 15/25 — option premiums need room; 8/12 triggered on noise.
# Phase 4 ExitPolicy replaces these with ATR-based absolute values at entry.
DEFAULT_SL_PCT              = float(os.environ.get("GUARDIAN_DEFAULT_SL_PCT", "15.0"))
DEFAULT_TP_PCT              = float(os.environ.get("GUARDIAN_DEFAULT_TP_PCT", "25.0"))
DEFAULT_TIME_EXIT_MINUTES   = int(os.environ.get("GUARDIAN_DEFAULT_TIME_EXIT_MIN", "30"))


# ── IST helpers ───────────────────────────────────────────────────────────────

def _ist_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _squareoff_due() -> bool:
    ist = _ist_now()
    if ist.weekday() >= 5:
        return False
    return ist.hour * 60 + ist.minute >= SQUAREOFF_MINUTE_IST


def _in_market_hours() -> bool:
    ist = _ist_now()
    if ist.weekday() >= 5:
        return False
    m = ist.hour * 60 + ist.minute
    return 9 * 60 + 15 <= m <= 15 * 60 + 30


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run_guardian_loop(
    db,
    stop_event: asyncio.Event,
    *,
    close_fn: Callable[..., Coroutine],
    quote_ltp_fn: Callable[..., Coroutine],
    get_ltp_fn: Callable[..., Coroutine],
    get_settings_fn: Callable[..., Coroutine],
) -> None:
    """
    close_fn(user_id, strategy_id, reason)              — fires the exit
    quote_ltp_fn(user_id, instrument_key) → float|None  — WS/REST LTP
    get_ltp_fn(user_id, symbol, exchange, allow_mock, execution_broker) → float|None
    get_settings_fn(user_id) → dict
    """
    logger.info("PositionGuardian started (poll=%ss, squareoff=%d:%02d IST)",
                GUARDIAN_POLL_SECONDS, SQUAREOFF_MINUTE_IST // 60, SQUAREOFF_MINUTE_IST % 60)
    while not stop_event.is_set():
        try:
            await _guardian_tick(db, close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn)
        except Exception as exc:
            logger.error("PositionGuardian tick error: %s", exc)
        slept = 0
        while not stop_event.is_set() and slept < GUARDIAN_POLL_SECONDS:
            await asyncio.sleep(1)
            slept += 1
    logger.info("PositionGuardian stopped")


# ── Guardian tick ─────────────────────────────────────────────────────────────

async def _guardian_tick(db, close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn) -> None:
    in_hours  = _in_market_hours()
    squareoff = _squareoff_due()

    rows = await db.strategy_positions.find(
        {"status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}},
        {"_id": 0},
    ).to_list(1000)

    for pos in rows:
        try:
            await _guard_one(
                db, pos, in_hours, squareoff,
                close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn,
            )
        except Exception as exc:
            logger.error(
                "PositionGuardian: error on pos=%s user=%s symbol=%s: %s",
                pos.get("id"), pos.get("user_id"),
                pos.get("target_symbol") or pos.get("symbol"), exc,
            )


async def _guard_one(
    db, pos: Dict[str, Any],
    in_hours: bool,
    squareoff: bool,
    close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn,
) -> None:
    user_id = pos.get("user_id")
    sid     = pos.get("strategy_id")
    symbol  = pos.get("target_symbol") or pos.get("trading_symbol") or pos.get("symbol")
    pos_id  = pos.get("id")
    if not user_id or not sid or not symbol or not pos_id:
        return

    # ── 1. Verify/assign SL + TP + deadline ───────────────────────────────────
    pos = await _ensure_risk_params(db, pos)

    # ── 2. Zombie alert ───────────────────────────────────────────────────────
    last_tick = pos.get("last_tick_at") or pos.get("updated_at") or pos.get("created_at")
    if last_tick:
        try:
            last_dt = datetime.fromisoformat(str(last_tick).replace("Z", "+00:00"))
            age_s = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if in_hours and age_s > MAX_UNMONITORED_SECONDS:
                logger.error(
                    "PositionGuardian ZOMBIE: pos=%s user=%s symbol=%s "
                    "has had no LTP update for %.0fs — taking over.",
                    pos_id, user_id, symbol, age_s,
                )
        except Exception:
            pass

    # ── 3. 15:10 force-close ──────────────────────────────────────────────────
    if squareoff:
        logger.warning(
            "PositionGuardian: squareoff-1510 for pos=%s user=%s symbol=%s",
            pos_id, user_id, symbol,
        )
        await close_fn(user_id, sid, reason="intraday-squareoff-1510")
        return

    if not in_hours:
        return

    # ── 4. Fetch LTP ──────────────────────────────────────────────────────────
    ltp, ltp_source = await _resolve_ltp_guardian(
        db, pos, quote_ltp_fn, get_ltp_fn, get_settings_fn
    )

    # ── 5. Deadline exceeded with no LTP → force MARKET exit ─────────────────
    deadline_str = pos.get("deadline_at")
    deadline_passed = False
    if deadline_str:
        try:
            dl = datetime.fromisoformat(str(deadline_str).replace("Z", "+00:00"))
            deadline_passed = datetime.now(timezone.utc) >= dl
        except Exception:
            pass

    if ltp is None:
        if deadline_passed:
            logger.error(
                "PositionGuardian: LTP unavailable AND deadline passed for pos=%s "
                "user=%s symbol=%s — forcing MARKET exit.",
                pos_id, user_id, symbol,
            )
            await close_fn(user_id, sid, reason="guardian-market-exit-ltp-unavailable")
        return

    # ── 6. Check exit conditions (SL / TP / trailing-SL / deadline / time) ────
    reason = exit_reason(pos, float(ltp))

    # Also honour the absolute deadline_at field (position_lifecycle checks
    # time_exit_minutes but not an explicit deadline_at timestamp).
    if not reason and deadline_passed:
        risk = normalize_strategy_risk(pos.get("tp_sl_tsl_config") or {})
        reason = f"time-exit-deadline"

    if reason:
        logger.info(
            "PositionGuardian: exit pos=%s user=%s symbol=%s reason=%s ltp=%.2f src=%s",
            pos_id, user_id, symbol, reason, float(ltp), ltp_source,
        )
        await close_fn(user_id, sid, reason=reason)


# ── Ensure risk params helper ─────────────────────────────────────────────────

async def _ensure_risk_params(db, pos: Dict[str, Any]) -> Dict[str, Any]:
    """Assigns default SL/TP/deadline if the position is UNPROTECTED."""
    risk_cfg = pos.get("tp_sl_tsl_config") or {}
    protection_status = str(risk_cfg.get("protection_status") or "").upper()
    has_sl = bool(risk_cfg.get("stop_loss_pct") or risk_cfg.get("stoploss_price") or risk_cfg.get("stop_loss"))
    has_tp = bool(risk_cfg.get("take_profit_pct") or risk_cfg.get("target_price") or risk_cfg.get("take_profit"))
    has_deadline = bool(pos.get("deadline_at"))

    if has_sl and has_tp and (has_deadline or risk_cfg.get("time_exit_minutes")):
        return pos  # fully protected — nothing to do

    entry_price = float(pos.get("average_buy_price") or pos.get("average_price") or 0)
    if entry_price <= 0:
        return pos  # can't compute without entry price

    updates: Dict[str, Any] = {}
    risk_cfg = dict(risk_cfg)

    if not has_sl:
        risk_cfg["stop_loss_pct"]  = DEFAULT_SL_PCT
        risk_cfg["stoploss_pct"]   = DEFAULT_SL_PCT
        risk_cfg["protection_status"] = "PROTECTED_GUARDIAN_DEFAULTS"
        logger.warning(
            "PositionGuardian: pos=%s user=%s symbol=%s missing SL — "
            "assigned default %.0f%%",
            pos.get("id"), pos.get("user_id"),
            pos.get("target_symbol") or pos.get("symbol"), DEFAULT_SL_PCT,
        )

    if not has_tp:
        risk_cfg["take_profit_pct"] = DEFAULT_TP_PCT
        risk_cfg["target_pct"]      = DEFAULT_TP_PCT
        risk_cfg["protection_status"] = risk_cfg.get("protection_status", "PROTECTED_GUARDIAN_DEFAULTS")

    if not has_deadline and not risk_cfg.get("time_exit_minutes"):
        risk_cfg["time_exit_minutes"] = DEFAULT_TIME_EXIT_MINUTES

    updates["tp_sl_tsl_config"] = risk_cfg

    if not has_deadline and risk_cfg.get("time_exit_minutes"):
        entry_time = pos.get("entry_time") or pos.get("created_at")
        if entry_time:
            try:
                entry_dt = datetime.fromisoformat(str(entry_time).replace("Z", "+00:00"))
                deadline = entry_dt + timedelta(minutes=int(risk_cfg["time_exit_minutes"]))
                updates["deadline_at"] = deadline.isoformat()
            except Exception:
                pass

    if updates:
        updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        await db.strategy_positions.update_one(
            {"id": pos["id"], "user_id": pos["user_id"]},
            {"$set": updates},
        )
        pos = {**pos, **updates}

    return pos


# ── LTP resolution (mirrors position_monitor chain) ──────────────────────────

_LTP_WS   = "WS_CACHE"
_LTP_SYM  = "SYMBOL_LTP"
_LTP_PCACHE = "PAPER_CACHE"
_LTP_ENTRY  = "ENTRY_PRICE_FALLBACK"


async def _resolve_ltp_guardian(
    db,
    pos: Dict[str, Any],
    quote_ltp_fn,
    get_ltp_fn,
    get_settings_fn,
) -> tuple[Optional[float], str]:
    return await resolve_position_ltp(
        db, pos, quote_ltp_fn, get_ltp_fn, get_settings_fn, allow_entry_fallback=False
    )
