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

from safe_exec import safe_run_strategy

logger = logging.getLogger("quantg.runner")

TICK_SECONDS = 30
LOCK_TTL_SECONDS = 90  # lock auto-expires if a pod dies
LOCK_ID = "strategy_runner"
POD_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _safe_run(code: str, data: List[dict]) -> List[dict]:
    """Run user strategy via shared AST-validated sandbox. Returns [] on error."""
    try:
        return safe_run_strategy(code, data)
    except Exception as e:
        logger.warning(f"strategy code error: {e}")
        return []


async def _acquire_lock(db) -> bool:
    """Try to grab the runner lock. Returns True if this pod owns it now."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=LOCK_TTL_SECONDS)
    try:
        # 1. Try to take an expired/abandoned lock or refresh our own
        res = await db.runner_locks.update_one(
            {
                "_id": LOCK_ID,
                "$or": [
                    {"expires_at": {"$lt": now}},
                    {"owner": POD_ID},
                ],
            },
            {"$set": {"owner": POD_ID, "expires_at": expires_at, "renewed_at": now}},
        )
        if res.matched_count == 1:
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
                      resolve_option_fn=None):
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
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
            continue
        try:
            strategies = await db.strategies.find({"status": "live"}).to_list(500)
        except Exception as e:
            logger.exception(f"runner loop error fetching strategies: {e}")
            strategies = []
        for s in strategies:
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
                    symbol = (vc.get("symbol") or "RELIANCE").upper()
                # last 60 daily candles for context
                try:
                    data = await get_price_history(s["user_id"], symbol, days=60)
                except Exception as e:
                    logger.warning(f"price history failed for {symbol}: {e}")
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set, "last_error": str(e)[:200]},
                                                    "$inc": inc_set})
                    continue
                if not data:
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": eval_set, "$inc": inc_set})
                    continue
                signals = _safe_run(code, data)
                signals_count = len(signals)
                if not signals:
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set, "last_signals_count": 0},
                                                    "$inc": inc_set})
                    continue
                last_sig = signals[-1]
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
                # Determine if this strategy trades OPTIONS instead of equity
                opt_cfg = (vc or {}).get("options") or {}
                option_contract = None
                if opt_cfg.get("enabled") and resolve_option_fn:
                    try:
                        option_contract = await resolve_option_fn(
                            user_id=s["user_id"],
                            underlying=opt_cfg.get("underlying", "NIFTY"),
                            signal_action=action,
                            strike_mode=opt_cfg.get("strike_mode", "ATM_BUY"),
                            otm_points=int(opt_cfg.get("otm_points") or 0),
                            expiry_offset=int(opt_cfg.get("expiry_offset") or 0),
                        )
                        if not option_contract:
                            await db.strategies.update_one(
                                {"id": s["id"]},
                                {"$set": {**eval_set,
                                          "last_error": "Options resolution failed (markets closed / no Kite session?)",
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
                        continue

                # Trigger order using injected fn — it applies paper_mode + risk limits
                try:
                    place_kwargs: Dict[str, Any] = dict(
                        user_id=s["user_id"],
                        symbol=symbol,
                        side=action,
                        qty=int(opt_cfg.get("lots") or 1) if option_contract else None,
                        order_type="MARKET",
                        product=None,
                        source=f"strategy:{s['id']}",
                    )
                    if option_contract:
                        place_kwargs["option_contract"] = option_contract
                    await place_order_fn(**place_kwargs)
                    log_target = option_contract["tradingsymbol"] if option_contract else symbol
                    await db.strategies.update_one(
                        {"id": s["id"]},
                        {"$set": {**eval_set,
                                  "last_signal_at": datetime.now(timezone.utc).isoformat(),
                                  "last_signal_action": action,
                                  "last_signals_count": signals_count,
                                  "last_fired_signal_date": last_sig_date,
                                  "last_traded_symbol": log_target},
                         "$inc": {**inc_set, "signals_fired": 1}},
                    )
                    logger.info(f"strategy {s['id']} → {action} {log_target}")
                except Exception as e:
                    logger.warning(f"order failed for strategy {s['id']}: {e}")
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
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    # Cleanup: release lock so another pod can take over immediately
    await _release_lock(db)
    logger.info("Strategy runner stopped")
