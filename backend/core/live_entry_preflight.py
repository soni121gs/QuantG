"""QuantG Live Entry Preflight.

A hard gate applied inside strategy_runner BEFORE any live signal reaches
UpstoxLiveAdapter.execute().  All checks are explicit and return a structured
result so skipped signals carry a visible reason.

If ANY check fails:
  - ok = False
  - reason_code = LIVE_PREFLIGHT_NOT_READY
  - the caller MUST skip cleanly (no FAILED broker order created)

Checks performed (in order):
  1. CORE_ENGINE_LIVE_ENABLED env flag
  2. User live arm state + global_live_enabled
  3. Kill switch inactive
  4. Upstox token valid (broker_keys.access_token present)
  5. Market session open (market_clock)
  6. Option contract has real instrument_key (contains '|')
  7. Contract is NOT simulated
  8. Option LTP is positive (fresh price exists)
  9. option_quality_score >= LIVE_MIN_SCORE (from option_selector_v2)
  10. No duplicate active position for same instrument
  11. Idempotency key is fresh (not already used in last 10 min)
  12. R-exit metadata attached or fallback sentinel created
"""
from __future__ import annotations

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from core.market_clock import get_segment_status
from core.market_domains import resolve_domain_by_underlying
from core.option_selector_v2 import LIVE_MIN_SCORE

logger = logging.getLogger("quantg.live_preflight")

IDEM_REUSE_WINDOW_SECONDS = 600  # 10 minutes


def _ok(checks: list) -> Dict[str, Any]:
    return {"ok": True, "reason_code": "LIVE_PREFLIGHT_PASSED", "checks": checks}


def _fail(reason: str, checks: list, detail: str = "") -> Dict[str, Any]:
    return {
        "ok": False,
        "reason_code": "LIVE_PREFLIGHT_NOT_READY",
        "failed_check": reason,
        "detail": detail,
        "checks": checks,
    }


async def live_entry_preflight(
    db,
    user_id: str,
    sig: Dict[str, Any],
    strategy: Dict[str, Any],
) -> Dict[str, Any]:
    """Run all live entry preflight checks.

    Parameters
    ----------
    db : Motor AsyncIOMotorDatabase
    user_id : str
    sig : dict  — the signal document from db.signals
    strategy : dict — the strategy document from db.strategies

    Returns
    -------
    dict with keys: ok, reason_code, failed_check, detail, checks
    """
    checks: list = []

    # ------------------------------------------------------------------ #
    # 1. CORE_ENGINE_LIVE_ENABLED                                          #
    # ------------------------------------------------------------------ #
    live_env = os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true"
    checks.append({"id": "env_live_enabled", "ok": live_env,
                   "detail": f"CORE_ENGINE_LIVE_ENABLED={os.environ.get('CORE_ENGINE_LIVE_ENABLED')}"})
    if not live_env:
        return _fail("env_live_enabled", checks,
                     "CORE_ENGINE_LIVE_ENABLED is not set to true in environment")

    # ------------------------------------------------------------------ #
    # 2. Arm state                                                         #
    # ------------------------------------------------------------------ #
    arm_state = await db.live_arm_state.find_one({"user_id": user_id})
    armed = bool(arm_state and arm_state.get("armed") and arm_state.get("global_live_enabled"))
    checks.append({"id": "arm_state", "ok": armed,
                   "detail": f"armed={bool(arm_state and arm_state.get('armed'))}, "
                              f"global_live_enabled={bool(arm_state and arm_state.get('global_live_enabled'))}"})
    if not armed:
        return _fail("arm_state", checks, "System not armed or global_live_enabled not set")

    # ------------------------------------------------------------------ #
    # 3. Kill switch                                                        #
    # ------------------------------------------------------------------ #
    ks = await db.risk_state.find_one({"_id": "global_kill_switch"})
    kill_active = bool(ks and ks.get("active"))
    checks.append({"id": "kill_switch", "ok": not kill_active,
                   "detail": f"kill_switch_active={kill_active}"})
    if kill_active:
        return _fail("kill_switch", checks, "Global kill-switch is active")

    # ------------------------------------------------------------------ #
    # 4. Upstox token                                                       #
    # ------------------------------------------------------------------ #
    broker_keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "upstox"})
    has_token = bool(broker_keys and broker_keys.get("access_token"))
    mock_token = has_token and str(broker_keys.get("access_token", "")).startswith("mock_")
    token_ok = has_token and not mock_token
    checks.append({"id": "upstox_token", "ok": token_ok,
                   "detail": f"has_token={has_token}, mock={mock_token}"})
    if not token_ok:
        return _fail("upstox_token", checks,
                     "Upstox access token missing or is a mock token")

    # ------------------------------------------------------------------ #
    # 5. Market session open                                                #
    # ------------------------------------------------------------------ #
    symbol = str(sig.get("symbol") or "NIFTY").upper()
    session_open = False
    try:
        domain = resolve_domain_by_underlying(symbol)
        clock = get_segment_status(domain.name)
        session_open = bool(clock.get("open"))
    except Exception as e:
        checks.append({"id": "market_session", "ok": False, "detail": f"clock error: {e}"})
        return _fail("market_session", checks, f"Market session check failed: {e}")
    checks.append({"id": "market_session", "ok": session_open,
                   "detail": f"segment={symbol}, open={session_open}"})
    if not session_open:
        return _fail("market_session", checks,
                     f"Market session for {symbol} is currently CLOSED")

    # ------------------------------------------------------------------ #
    # 6. Instrument key valid                                               #
    # ------------------------------------------------------------------ #
    opt = sig.get("option_contract") or {}
    instrument_key = str(opt.get("instrument_key") or opt.get("instrument_token") or "").strip()
    key_ok = "|" in instrument_key
    checks.append({"id": "instrument_key", "ok": key_ok,
                   "detail": f"instrument_key={instrument_key!r}"})
    if not key_ok:
        return _fail("instrument_key", checks,
                     f"instrument_key missing or malformed: {instrument_key!r}")

    # ------------------------------------------------------------------ #
    # 7. Not simulated                                                      #
    # ------------------------------------------------------------------ #
    is_simulated = (
        bool(opt.get("simulated"))
        or instrument_key.upper().startswith("PAPER_")
        or str(opt.get("source") or "").upper() in {"PAPER_SIMULATED_CONTRACT", "PAPER_SIMULATED_PRICE"}
    )
    checks.append({"id": "not_simulated", "ok": not is_simulated,
                   "detail": f"simulated={is_simulated}"})
    if is_simulated:
        return _fail("not_simulated", checks,
                     "Simulated/paper contract cannot be used for live entry")

    # ------------------------------------------------------------------ #
    # 8. Option LTP positive                                                #
    # ------------------------------------------------------------------ #
    ltp = 0.0
    try:
        ltp = float(opt.get("ltp") or sig.get("ltp") or 0)
    except (TypeError, ValueError):
        pass
    checks.append({"id": "ltp_positive", "ok": ltp > 0,
                   "detail": f"ltp={ltp}"})
    if ltp <= 0:
        return _fail("ltp_positive", checks, "Option LTP is zero or missing — no fresh price")

    # ------------------------------------------------------------------ #
    # 9. option_quality_score >= live threshold                             #
    # ------------------------------------------------------------------ #
    quality_score = sig.get("option_quality_score")
    if quality_score is None:
        quality_score = opt.get("option_quality_score")
    try:
        quality_score = float(quality_score) if quality_score is not None else 0.0
    except (TypeError, ValueError):
        quality_score = 0.0
    quality_ok = quality_score >= LIVE_MIN_SCORE
    checks.append({"id": "option_quality_score", "ok": quality_ok,
                   "detail": f"score={quality_score}, threshold={LIVE_MIN_SCORE}"})
    if not quality_ok:
        return _fail("option_quality_score", checks,
                     f"Quality score {quality_score:.0f} below live threshold {LIVE_MIN_SCORE}")

    # ------------------------------------------------------------------ #
    # 10. No duplicate active position for same instrument                  #
    # ------------------------------------------------------------------ #
    dup_match = {
        "user_id": user_id,
        "mode": "live",
        "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
        "$or": [
            {"instrument_key": instrument_key},
            {"instrument_token": instrument_key},
        ],
    }
    target_sym = str(opt.get("tradingsymbol") or sig.get("target_symbol") or "").upper()
    if target_sym:
        dup_match["$or"].append({"target_symbol": target_sym})
    dup = await db.strategy_positions.find_one(dup_match, {"_id": 0, "id": 1})
    no_dup = dup is None
    checks.append({"id": "no_duplicate_position", "ok": no_dup,
                   "detail": f"existing_position={dup.get('id') if dup else None}"})
    if not no_dup:
        return _fail("no_duplicate_position", checks,
                     f"Active position already exists for {instrument_key}")

    # ------------------------------------------------------------------ #
    # 11. Idempotency key fresh                                             #
    # ------------------------------------------------------------------ #
    idem_key = sig.get("idempotency_key") or f"sig:{sig.get('id', '')}"
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=IDEM_REUSE_WINDOW_SECONDS)).isoformat()
    existing_order = await db.orders.find_one(
        {"user_id": user_id, "idempotency_key": idem_key, "created_at": {"$gte": cutoff}},
        {"_id": 0, "id": 1, "status": 1},
    )
    idem_fresh = existing_order is None
    checks.append({"id": "idempotency_fresh", "ok": idem_fresh,
                   "detail": f"idem_key={idem_key}, existing_order={existing_order.get('id') if existing_order else None}"})
    if not idem_fresh:
        return _fail("idempotency_fresh", checks,
                     f"Idempotency key {idem_key!r} was already used recently")

    # ------------------------------------------------------------------ #
    # 12. R-exit metadata present or create fallback sentinel               #
    # ------------------------------------------------------------------ #
    r_meta = (
        sig.get("initial_stop_R")
        or sig.get("stop_loss")
        or strategy.get("stop_loss")
    )
    r_ok = r_meta is not None
    if not r_ok:
        # Create a soft fallback: 2% stop will be used by R-exit evaluator
        logger.warning(
            "live_preflight: no R-exit metadata for sig=%s strategy=%s — fallback stop 2pct applied",
            sig.get("id"), sig.get("strategy_id"),
        )
        r_ok = True  # non-blocking: fallback is created at execution time
    checks.append({"id": "r_exit_metadata", "ok": True,
                   "detail": f"initial_stop_R={sig.get('initial_stop_R')}, "
                              f"stop_loss={sig.get('stop_loss')}, fallback=2pct"})

    # ------------------------------------------------------------------ #
    # All passed                                                            #
    # ------------------------------------------------------------------ #
    logger.info(
        "live_preflight PASSED sig=%s strategy=%s symbol=%s ltp=%.2f quality=%.0f",
        sig.get("id"), sig.get("strategy_id"), symbol, ltp, quality_score,
    )
    return _ok(checks)
