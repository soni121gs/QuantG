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
import session_times
from core.position_lifecycle import (
    exit_reason,
    normalize_strategy_risk,
    position_risk_prices,
    parse_iso_dt,
)

logger = logging.getLogger("quantg.position_guardian")

# ── Config (all overridable via env) ──────────────────────────────────────────

GUARDIAN_POLL_SECONDS       = int(os.environ.get("GUARDIAN_POLL_SECONDS", "5"))
# Must complete BEFORE the 15:15 cash Closing Auction Session (2026-08-03).
SQUAREOFF_MINUTE_IST        = int(os.environ.get(
    "GUARDIAN_SQUAREOFF_MINUTE", str(session_times.EQUITY_SQUAREOFF_MINUTE)))
MAX_UNMONITORED_SECONDS     = int(os.environ.get("GUARDIAN_MAX_UNMONITORED_SEC", "120"))
# Widened to 15/25 — option premiums need room; 8/12 triggered on noise.
# Phase 4 ExitPolicy replaces these with ATR-based absolute values at entry.
DEFAULT_SL_PCT              = float(os.environ.get("GUARDIAN_DEFAULT_SL_PCT", "15.0"))
DEFAULT_TP_PCT              = float(os.environ.get("GUARDIAN_DEFAULT_TP_PCT", "25.0"))
DEFAULT_TIME_EXIT_MINUTES   = int(os.environ.get("GUARDIAN_DEFAULT_TIME_EXIT_MIN", "30"))
# Phantom equity-quote guard — see position_monitor._EQUITY_LTP_MAX_DEV. Reject an
# implausible equity LTP (bad open-auction tick) before it fires a phantom SL exit.
EQUITY_LTP_MAX_DEV          = float(os.environ.get("EQUITY_LTP_MAX_DEV", "0.35"))


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
    return session_times.OPEN_MINUTE <= m <= session_times.LAST_CLOSE_MINUTE


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

    # Credit/debit spreads are valued from BOTH legs and are owned exclusively by
    # position_monitor._process_spread_position. The guardian's single-leg LTP /
    # staleness logic cannot price a spread: it has no top-level instrument_key,
    # yet a top-level option_type ("PE"/"CE") wrongly trips the option staleness
    # guard, so every spread fell back to entry-price, never refreshed
    # last_fresh_tick_at, and was force-closed at the 300s stale threshold
    # (reason=stale-quote-protective-exit) — almost always at a loss. Let the
    # monitor (which prices both legs) own the spread lifecycle.
    if str(pos.get("structure")) in ("credit_spread", "debit_spread"):
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

    # ── Phantom equity-quote guard ────────────────────────────────────────────
    # Reject an implausible equity LTP (suspected bad open-auction tick) before it
    # can trigger a phantom SL/TP exit (mirrors position_monitor).
    if (
        ltp is not None
        and ltp_source not in ("ENTRY_PRICE_FALLBACK", "NONE")
        and str(pos.get("asset_type") or "").lower() == "equity"
    ):
        _entry_ref = float(pos.get("average_buy_price") or pos.get("average_price") or pos.get("entry_price") or 0)
        if _entry_ref > 0 and abs(float(ltp) / _entry_ref - 1.0) > EQUITY_LTP_MAX_DEV:
            _dev = abs(float(ltp) / _entry_ref - 1.0)
            _checked_at = datetime.now(timezone.utc).isoformat()
            _diagnostic = {
                "source": ltp_source,
                "rejected_ltp": float(ltp),
                "entry_ref": _entry_ref,
                "deviation_pct": round(_dev * 100, 2),
                "symbol": symbol,
                "instrument_key": pos.get("instrument_key") or pos.get("instrument_token"),
                "checked_at": _checked_at,
                "guard": "position_guardian",
            }
            logger.warning(
                "position_guardian: rejecting phantom equity ltp=%.2f for %s (entry=%.2f dev=%.0f%% src=%s) — skipping exit",
                float(ltp), symbol, _entry_ref, _dev * 100, ltp_source,
            )
            await db.strategy_positions.update_one(
                {"id": pos_id, "user_id": user_id, "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}},
                {"$set": {
                    "last_error": f"PHANTOM_LTP_REJECTED: {float(ltp):.2f} vs entry {_entry_ref:.2f} ({_dev * 100:.0f}%)",
                    "equity_ltp_diagnostic": _diagnostic,
                    "phantom_ltp_rejected_at": _checked_at,
                    "phantom_ltp_source": ltp_source,
                    "phantom_ltp_value": float(ltp),
                    "phantom_ltp_entry_ref": _entry_ref,
                    "phantom_ltp_deviation_pct": round(_dev * 100, 2),
                    "updated_at": _checked_at,
                }},
            )
            return

    # ── Staleness protective exit (TASK-P-EX02) ───────────────────────────────
    # Spreads are excluded here too (defense-in-depth alongside the structure
    # skip at the top of _guard_one): their top-level option_type must not trip
    # the single-leg staleness guard. position_monitor owns spread exits.
    is_spread = (
        str(pos.get("structure") or "") in ("credit_spread", "debit_spread")
        or str(pos.get("asset_type") or "").lower() == "option_spread"
    )
    is_option_or_equity = (not is_spread) and (
        str(pos.get("asset_type") or "").lower() in ("option", "equity")
        or pos.get("exchange") in ("NFO", "BFO", "NSE", "BSE")
        or str(pos.get("trading_symbol") or "").endswith(("CE", "PE"))
        or str(pos.get("option_type") or "").upper() in ("CE", "PE")
        or "_EQ|" in str(pos.get("instrument_key") or "")
    )
    if is_option_or_equity and pos.get("status") in ("OPEN", "FILLED"):
        last_fresh_str = pos.get("last_fresh_tick_at") or pos.get("entry_time") or pos.get("created_at")
        last_fresh_dt = parse_iso_dt(last_fresh_str)
        if last_fresh_dt:
            elapsed = (datetime.now(timezone.utc) - last_fresh_dt).total_seconds()
            from config import MONITOR
            stale_threshold = getattr(MONITOR, "OPTION_LTP_STALE_EXIT_SECONDS", 300)
            if elapsed > stale_threshold:
                if ltp_source in ("ENTRY_PRICE_FALLBACK", "NONE") or ltp is None:
                    fresh_ltp = None
                    ikey = pos.get("instrument_key") or pos.get("instrument_token")
                    from ltp_resolver import should_trust_ws_cache
                    if ikey and should_trust_ws_cache(ikey):
                        try:
                            fresh_ltp = await quote_ltp_fn(user_id, ikey)
                        except Exception as e:
                            logger.warning("stale check REST fallback failed for %s in guardian: %s", symbol, e)
                    if fresh_ltp is not None:
                        ltp = float(fresh_ltp)
                        ltp_source = "REST_FALLBACK"
                        # Update DB to prevent immediate re-evaluation on next guardian poll.
                        # Status-guarded: position_monitor's close_fn may have closed this
                        # position while we were awaiting the REST quote above — don't
                        # restamp ltp fields onto an already CLOSED/EXITING doc.
                        from core.portfolio_ledger import PortfolioLedger
                        await PortfolioLedger(db).update_position_mark(
                            position_id=pos_id, user_id=user_id,
                            allowed_statuses=("OPEN", "FILLED"),
                            fields={"last_fresh_tick_at": datetime.now(timezone.utc).isoformat(),
                                    "last_ltp": ltp, "ltp_source": ltp_source,
                                    "updated_at": datetime.now(timezone.utc).isoformat()},
                        )
                    else:
                        logger.warning(
                            "position_guardian: OPEN position %s user=%s symbol=%s has no fresh LTP for %.0fs — forcing protective exit.",
                            pos_id, user_id, symbol, elapsed,
                        )
                        await close_fn(user_id, sid, reason="stale-quote-protective-exit", decided_ltp=ltp)
                        return

    # If the quote is fresh and not fallback, update last_fresh_tick_at in DB
    # but rate-limited to avoid DB write spam (at most once every 10 seconds).
    if ltp is not None and ltp_source not in ("ENTRY_PRICE_FALLBACK", "NONE"):
        last_fresh_str = pos.get("last_fresh_tick_at")
        should_update = True
        if last_fresh_str:
            last_fresh_dt = parse_iso_dt(last_fresh_str)
            if last_fresh_dt and (datetime.now(timezone.utc) - last_fresh_dt).total_seconds() < 10:
                should_update = False
        if should_update:
            # Status-guarded for the same reason as above.
            from core.portfolio_ledger import PortfolioLedger
            await PortfolioLedger(db).update_position_mark(
                position_id=pos_id, user_id=user_id,
                allowed_statuses=("OPEN", "FILLED"),
                fields={"last_fresh_tick_at": datetime.now(timezone.utc).isoformat()},
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
        await close_fn(user_id, sid, reason=reason, decided_ltp=float(ltp))


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
