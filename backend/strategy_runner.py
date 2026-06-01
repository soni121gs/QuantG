"""Background strategy runner.

Single asyncio task started on app startup. Every TICK_SECONDS, iterates
through all strategies with status="live", fetches a short price history,
runs the user's `run(data)` function and acts on the last signal.

Multi-replica safety: uses a Mongo-based distributed lock with TTL. Only
the pod currently holding the lock runs the runner — prevents duplicate
order placement when the deployment scales to >1 replica.

For safety: this runner ALWAYS goes through `/orders` business logic which
respects user.paper_mode and user.max_position_size — so flipping the
master switch on the profile is honoured automatically.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

from pymongo import ReturnDocument

from safe_exec import safe_run_strategy
from market_protection import MarketTrendAnalyzer, FakeSignalFilter

logger = logging.getLogger("quantg.runner")

TICK_SECONDS = int(os.environ.get("STRATEGY_RUNNER_TICK_SECONDS", "15"))
LOCK_TTL_SECONDS = 90  # lock auto-expires if a pod dies
LOCK_ID = "strategy_runner"
POD_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
SIGNAL_CONFIDENCE_MIN = float(os.environ.get("SIGNAL_CONFIDENCE_MIN", "45"))
NON_ERROR_ENTRY_BLOCKS = (
    "cooldown-active",
    "duplicate-buy-dropped",
    "max-trades-day-reached",
    "New BUY blocked",
    "Duplicate BUY blocked",
    "Re-entry blocked",
    "already has active",
    "Live LTP unavailable",
    "Insufficient funds",
    "insufficient funds",
    "margin",
    "Margin",
)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: int) -> None:
    slept = 0
    while not stop_event.is_set() and slept < seconds:
        await asyncio.sleep(1)
        slept += 1


def _safe_run(code: str, data: List[dict]) -> List[dict]:
    """Run user strategy via shared AST-validated sandbox. Returns [] on error."""
    try:
        return safe_run_strategy(code, data)
    except Exception as e:
        logger.warning(f"strategy code error: {e}")
        return []


def _validate_signal(signal: Dict[str, Any], data: List[dict], strategy: Dict[str, Any] = None) -> Dict[str, Any]:
    try:
        trend = MarketTrendAnalyzer.analyze(data, lookback=min(50, max(20, len(data))))
        is_hft = False
        if strategy:
            name = str(strategy.get("name") or "").lower()
            desc = str(strategy.get("description") or "").lower()
            if "hft" in name or "hft" in desc or "scalper" in name or "scalper" in desc:
                is_hft = True
        validation = FakeSignalFilter.validate(signal, data, trend, is_hft=is_hft)
        threshold = 35.0 if is_hft else SIGNAL_CONFIDENCE_MIN
        validation["threshold"] = threshold
        validation["trend"] = trend
        validation["is_valid"] = bool(validation.get("is_valid")) and float(validation.get("confidence", 0)) >= threshold
        return validation
    except Exception as e:
        logger.warning(f"signal validation failed: {e}")
        return {
            "is_valid": False,
            "confidence": 0,
            "threshold": SIGNAL_CONFIDENCE_MIN,
            "reasons": [f"Validation failed: {e}"],
            "filtered": True,
            "trend": {},
        }


def _entry_block_reason(exc: Exception) -> str | None:
    status_code = getattr(exc, "status_code", None)
    detail = str(getattr(exc, "detail", "") or exc)
    if status_code not in (None, 400, 409):
        return None
    if not any(prefix in detail or prefix in str(exc) for prefix in (
        "Option entry blocked:",
        "Strategy entry blocked:",
        "Instrument already has active strategy position:",
        "Strategy already has active position",
        "Instrument/strategy already reserved",
        "Live LTP unavailable",
        "Insufficient funds",
        "insufficient funds",
        "margin",
        "Margin",
    )):
        return None
    for reason in NON_ERROR_ENTRY_BLOCKS:
        if reason in detail or reason in str(exc):
            return reason
    return None


async def _acquire_lock(db) -> bool:
    """Try to grab the runner lock. Returns True if this pod owns it now."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=LOCK_TTL_SECONDS)
    try:
        # 1. Try to take an expired/abandoned lock or refresh our own
        row = await db.runner_locks.find_one_and_update(
            {
                "_id": LOCK_ID,
                "$or": [
                    {"expires_at": {"$lt": now}},
                    {"owner": POD_ID},
                ],
            },
            {"$set": {"owner": POD_ID, "expires_at": expires_at, "renewed_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if row:
            return True
        # 2. No existing lock at all — try to create one. If another pod creates
        #    it first we'll hit a duplicate-key error which we treat as "not us".
        try:
            await db.runner_locks.insert_one({
                "_id": LOCK_ID, "owner": POD_ID,
                "expires_at": expires_at, "renewed_at": now,
            })
            return True
        except Exception:
            # Another pod has the lock — perfectly fine, we'll retry next tick.
            return False
    except Exception as e:
        logger.warning(f"lock acquire failed: {e}")
        return False


async def _release_lock(db) -> None:
    try:
        await db.runner_locks.delete_one({"_id": LOCK_ID, "owner": POD_ID})
    except Exception:
        pass


async def runner_loop(db, get_price_history, place_order_fn, stop_event: asyncio.Event,
                      resolve_option_fn=None, close_strategy_fn=None):
    """Main loop. Dependencies injected to avoid circular imports.

    resolve_option_fn(user_id, underlying, signal_action, strike_mode, otm_points,
                      expiry_offset) -> contract_dict | None
    When provided and a strategy has visual_config.options.enabled=True, signals
    are translated to option contracts and place_order_fn is called with
    option_contract kwarg.
    """
    logger.info(f"Strategy runner starting (tick={TICK_SECONDS}s, pod={POD_ID})")
    while not stop_event.is_set():
        owns_lock = await _acquire_lock(db)
        if not owns_lock:
            # Another pod is leader; skip this tick.
            await _sleep_or_stop(stop_event, TICK_SECONDS)
            continue
        try:
            strategies = await db.strategies.find({"status": "live"}).to_list(500)
        except Exception as e:
            logger.exception(f"runner loop error fetching strategies: {e}")
            strategies = []
        for idx, s in enumerate(strategies):
            # Renew the distributed lock every 20 strategies to prevent TTL expiry
            # during a large batch (90s TTL can expire if each strategy takes ~450ms).
            if idx > 0 and idx % 20 == 0:
                renewed = await _acquire_lock(db)
                if not renewed:
                    logger.warning("runner lost lock mid-batch at strategy index %d — stopping tick early", idx)
                    break
            try:
                code = s.get("python_code") or ""
                eval_set: Dict[str, Any] = {
                    "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
                    "last_pod": POD_ID,
                }
                # increment scan counter
                inc_set: Dict[str, Any] = {"evaluations": 1}
                if not code:
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": eval_set, "$inc": inc_set})
                    continue
                if s.get("halted") or s.get("is_halted"):
                    await db.strategies.update_one(
                        {"id": s["id"]},
                        {"$set": {**eval_set, "last_filter_reason": s.get("halt_reason") or "Strategy halted."},
                         "$inc": inc_set},
                    )
                    continue
                # Resolve the symbol whose PRICE HISTORY the strategy will analyse.
                # When options mode is enabled, we MUST evaluate the strategy
                # against the UNDERLYING's spot price (NIFTY/BANKNIFTY/SENSEX) —
                # not against an unrelated equity symbol like RELIANCE. The
                # equity `symbol` field is only used in equity mode.
                vc = s.get("visual_config") or {}

                opt_cfg_early = (vc or {}).get("options") or {}
                if opt_cfg_early.get("enabled"):
                    symbol = (opt_cfg_early.get("underlying") or "NIFTY").upper()
                else:
                    raw_sym = vc.get("symbol")
                    if not raw_sym:
                        # No symbol configured — skip with a clear error rather than silently
                        # evaluating against a wrong default. This prevents RELIANCE candles
                        # being used as a proxy for an unrelated strategy.
                        await db.strategies.update_one({"id": s["id"]},
                                                       {"$set": {**eval_set, "last_error": "No symbol configured. Set a trading symbol in strategy settings to enable scanning."},
                                                        "$inc": inc_set})
                        continue
                    symbol = raw_sym.upper()
                # last 60 daily candles for context
                try:
                    history = await get_price_history(s["user_id"], symbol, days=60, strategy=s)
                except Exception as e:
                    logger.warning(f"price history failed for {symbol}: {e}")
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set, "last_error": str(e)[:200]},
                                                    "$inc": inc_set})
                    continue
                data = history
                if isinstance(history, dict):
                    data = history.get("data") or []
                    eval_set["last_data_source"] = history.get("source", "unknown")
                    eval_set["last_data_live"] = bool(history.get("is_live"))
                    eval_set["last_data_reason"] = history.get("live_reason")
                    eval_set["last_candle_at"] = history.get("last_candle_at")
                    eval_set["latest_candle_age_sec"] = history.get("latest_candle_age_sec")
                if not data:
                    error = None
                    if isinstance(history, dict) and not history.get("paper_mode", True):
                        source = history.get("source", "none")
                        error = f"No real Upstox price history available yet (source={source}). Reconnect Upstox and restart the feed."
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set, "last_error": error}, "$inc": inc_set})
                    continue
                signals = _safe_run(code, data)
                signals_count = len(signals)
                if not signals:
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {
                                                       **eval_set,
                                                       "last_signals_count": 0,
                                                       "last_filter_reason": (
                                                           f"No current setup from strategy code "
                                                           f"(candles={len(data)}, source={eval_set.get('last_data_source', 'unknown')})."
                                                       ),
                                                   },
                                                    "$inc": inc_set})
                    continue
                last_sig = signals[-1]
                is_paper_mode = bool(history.get("paper_mode", True)) if isinstance(history, dict) else False
                if not is_paper_mode and not bool(history.get("is_live") if isinstance(history, dict) else False):
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set,
                                                             "last_error": "Mock price history; live strategy execution blocked until real Upstox data is available.",
                                                             "last_signals_count": len(signals)},
                                                    "$inc": inc_set})
                    continue
                last_sig_date = last_sig.get("date", "")
                last_fired_date = s.get("last_fired_signal_date", "")

                # Don't re-fire the same signal we already acted on
                if last_sig_date and last_sig_date == last_fired_date:
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set, "last_signals_count": signals_count},
                                                    "$inc": inc_set})
                    continue

                # Allow signals from the last 3 candles (~15 min on 5-min bars).
                # Anything older is considered stale and ignored — prevents firing
                # ancient signals on a runner restart.
                recent_dates = {d.get("date") for d in data[-3:]}
                if last_sig_date not in recent_dates:
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set, "last_signals_count": signals_count},
                                                    "$inc": inc_set})
                    continue

                action = (last_sig.get("action") or "").upper()
                if action not in ("BUY", "SELL"):
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set, "last_signals_count": signals_count},
                                                    "$inc": inc_set})
                    continue
                signal_validation = _validate_signal(last_sig, data, s)
                if not signal_validation.get("is_valid"):
                    reason = "; ".join(signal_validation.get("reasons") or [])
                    await db.strategies.update_one(
                        {"id": s["id"]},
                        {"$set": {**eval_set,
                                  "last_signals_count": signals_count,
                                  "last_signal_action": action,
                                  "last_filter_reason": (
                                      f"Signal filtered: confidence {signal_validation.get('confidence')} "
                                      f"< {signal_validation.get('threshold')}. {reason}"
                                  ),
                                  "last_signal_validation": signal_validation},
                         "$unset": {"last_error": ""},
                         "$inc": inc_set},
                    )
                    continue
                # Determine if this strategy trades OPTIONS instead of equity
                opt_cfg = (vc or {}).get("options") or {}
                option_contract = None
                instrument_type = str(
                    opt_cfg.get("instrument_type")
                    or opt_cfg.get("contract_type")
                    or s.get("instrument_type")
                    or s.get("asset_class")
                    or ""
                ).upper()
                option_resolution_requested = bool(opt_cfg.get("enabled")) and instrument_type not in {"FUTURE", "FUTURES", "FUTCOM", "COMMODITY_FUTURE"}
                option_buying_mode = option_resolution_requested and str(opt_cfg.get("strike_mode") or "").upper().endswith("BUY")
                if option_buying_mode and action == "SELL":
                    # In option-buying strategies a SELL signal has two meanings:
                    # with an active position it is an exit, while flat it is a PE entry.
                    active_position = await db.strategy_positions.find_one(
                        {
                            "user_id": s["user_id"],
                            "strategy_id": s["id"],
                            "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
                        },
                        {"_id": 0, "id": 1},
                    )
                    if active_position and close_strategy_fn:
                        await close_strategy_fn(s["user_id"], s["id"], reason="strategy-sell-signal")
                        await db.strategies.update_one(
                            {"id": s["id"]},
                            {"$set": {**eval_set,
                                      "last_signal_action": action,
                                      "last_signals_count": signals_count,
                                      "last_filter_reason": "SELL signal used as option-buying exit."},
                             "$inc": inc_set},
                        )
                        continue
                if option_resolution_requested and resolve_option_fn:
                    try:
                        option_contract = await resolve_option_fn(
                            user_id=s["user_id"],
                            underlying=opt_cfg.get("underlying", "NIFTY"),
                            signal_action=action,
                            strike_mode=opt_cfg.get("strike_mode", "ATM_BUY"),
                            otm_points=int(opt_cfg.get("otm_points") or 0),
                            expiry_offset=int(opt_cfg.get("expiry_offset") or 0),
                            strategy=s,
                        )
                        if not option_contract:
                            underlying_name = opt_cfg.get("underlying", "NIFTY")
                            is_mcx_underlying = str(underlying_name).upper() in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"}
                            clear_reason = (
                                f"{underlying_name} contract unresolved: check Upstox MCX_FO permission / instrument master."
                                if is_mcx_underlying else
                                f"Upstox option contract resolution failed for {underlying_name}. Check OAuth, exchange segment permission, and instrument search logs."
                            )
                            await db.strategies.update_one(
                                {"id": s["id"]},
                                {"$set": {**eval_set,
                                          "last_error": clear_reason,
                                          "halted": True,
                                          "halt_reason": "CONTRACT_RESOLUTION_FAILED",
                                          "last_signals_count": signals_count},
                                 "$inc": inc_set})
                            continue
                    except Exception as e:
                        logger.warning(f"option resolve failed for {s['id']}: {e}")
                        await db.strategies.update_one(
                            {"id": s["id"]},
                            {"$set": {**eval_set, "last_error": str(e)[:200],
                                      "last_signals_count": signals_count},
                             "$inc": inc_set})
                                  # Insert signal into db.signals collection instead of placing order directly
                try:
                    target_symbol = option_contract["tradingsymbol"] if option_contract else symbol
                    option_type = option_contract.get("option_type") if option_contract else None
                    signal_id = str(uuid.uuid4())
                    now_str = datetime.now(timezone.utc).isoformat()
                    
                    signal_doc = {
                        "id": signal_id,
                        "user_id": s["user_id"],
                        "strategy_id": s["id"],
                        "mode": "paper" if is_paper_mode else "live",
                        "symbol": symbol,
                        "target_symbol": target_symbol,
                        "option_type": option_type,
                        "action": action,
                        "confidence": float(signal_validation.get("confidence", 85.0)),
                        "trend_context": signal_validation.get("trend") or {},
                        "visual_config": s.get("visual_config") or {},
                        "option_contract": option_contract,
                        "exchange": (option_contract.get("exchange") if option_contract else ("MCX" if str(symbol).upper() in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"} else "NSE")),
                        "status": "PENDING",
                        "rejection_reason": None,
                        "order_id": None,
                        "created_at": now_str,
                        "processed_at": None,
                    }
                    
                    await db.signals.insert_one(signal_doc)
                    
                    await db.strategies.update_one(
                        {"id": s["id"]},
                        {"$set": {**eval_set,
                                  "last_signal_action": action,
                                  "last_signals_count": signals_count,
                                  "last_fired_signal_date": last_sig_date,
                                  "last_traded_symbol": target_symbol},
                         "$inc": {**inc_set, "signals_fired": 1}},
                    )
                    logger.info(f"strategy {s['id']} → queued PENDING {action} signal {signal_id} for {target_symbol}")
                except Exception as e:
                    logger.warning(f"Failed to queue signal for strategy {s['id']}: {e}")
                    await db.strategies.update_one(
                        {"id": s["id"]},
                        {"$set": {**eval_set, "last_error": str(e)[:200]},
                         "$inc": inc_set})
            except Exception as e:
                logger.warning(f"strategy {s.get('id','?')} eval failed: {e}")
                try:
                    await db.strategies.update_one(
                        {"id": s.get("id")},
                        {"$set": {"last_evaluated_at": datetime.now(timezone.utc).isoformat(),
                                  "last_error": str(e)[:200]},
                         "$inc": {"evaluations": 1}},
                    )
                except Exception:
                    pass
        await _sleep_or_stop(stop_event, TICK_SECONDS)
    # Cleanup: release lock so another pod can take over immediately
    await _release_lock(db)
    logger.info("Strategy runner stopped")
