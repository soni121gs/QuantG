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


async def runner_loop(db, get_price_history, place_order_fn, stop_event: asyncio.Event):
    """Main loop. Dependencies injected to avoid circular imports."""
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
            for s in strategies:
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
                # default symbol
                symbol = "RELIANCE"
                vc = s.get("visual_config") or {}
                if vc.get("symbol"):
                    symbol = vc["symbol"]
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
                # only act if the latest signal is "today's" (most recent candle)
                if last_sig.get("date") != data[-1]["date"]:
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
                # Trigger order using injected fn — it applies paper_mode + risk limits
                try:
                    await place_order_fn(
                        user_id=s["user_id"],
                        symbol=symbol,
                        side=action,
                        qty=None,  # auto-uses user default_qty
                        order_type="MARKET",
                        product=None,  # auto-uses user default_product
                        source=f"strategy:{s['id']}",
                    )
                    await db.strategies.update_one(
                        {"id": s["id"]},
                        {"$set": {**eval_set,
                                  "last_signal_at": datetime.now(timezone.utc).isoformat(),
                                  "last_signal_action": action,
                                  "last_signals_count": signals_count},
                         "$inc": {**inc_set, "signals_fired": 1}},
                    )
                    logger.info(f"strategy {s['id']} → {action} {symbol}")
                except Exception as e:
                    logger.warning(f"order failed for strategy {s['id']}: {e}")
        except Exception as e:
            logger.exception(f"runner loop error: {e}")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    # Cleanup: release lock so another pod can take over immediately
    await _release_lock(db)
    logger.info("Strategy runner stopped")
