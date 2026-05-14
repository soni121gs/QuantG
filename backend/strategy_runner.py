"""Background strategy runner.

Single asyncio task started on app startup. Every TICK_SECONDS, iterates
through all strategies with status="live", fetches a short price history,
runs the user's `run(data)` function and acts on the last signal.

For safety: this runner ALWAYS goes through `/orders` business logic which
respects user.paper_mode and user.max_position_size — so flipping the
master switch on the profile is honoured automatically.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List

from safe_exec import safe_run_strategy

logger = logging.getLogger("quantg.runner")

TICK_SECONDS = 30


def _safe_run(code: str, data: List[dict]) -> List[dict]:
    """Run user strategy via shared AST-validated sandbox. Returns [] on error."""
    try:
        return safe_run_strategy(code, data)
    except Exception as e:
        logger.warning(f"strategy code error: {e}")
        return []


async def runner_loop(db, get_price_history, place_order_fn, stop_event: asyncio.Event):
    """Main loop. Dependencies injected to avoid circular imports."""
    logger.info(f"Strategy runner starting (tick={TICK_SECONDS}s)")
    while not stop_event.is_set():
        try:
            strategies = await db.strategies.find({"status": "live"}).to_list(500)
            for s in strategies:
                code = s.get("python_code") or ""
                if not code:
                    continue
                # default symbol
                symbol = "RELIANCE"
                if s.get("visual_config", {}).get("symbol"):
                    symbol = s["visual_config"]["symbol"]
                # last 60 daily candles for context
                try:
                    data = await get_price_history(s["user_id"], symbol, days=60)
                except Exception as e:
                    logger.warning(f"price history failed for {symbol}: {e}")
                    continue
                if not data:
                    continue
                signals = _safe_run(code, data)
                if not signals:
                    continue
                last_sig = signals[-1]
                # only act if the latest signal is "today's" (most recent candle)
                if last_sig.get("date") != data[-1]["date"]:
                    continue
                action = (last_sig.get("action") or "").upper()
                if action not in ("BUY", "SELL"):
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
                        {"$set": {"last_signal_at": datetime.now(timezone.utc).isoformat(),
                                  "last_signal_action": action}},
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
    logger.info("Strategy runner stopped")
