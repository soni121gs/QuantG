"""Position monitor loop — extracted from server.py.

Runs every 30 seconds. For every OPEN/FILLED/PENDING_BROKER position:
  1. Fetches live LTP from Upstox V3 feed (or falls back to _get_ltp_fn).
  2. Updates last_ltp, unrealized_pnl, and risk prices in DB.
  3. If in IST market hours AND exit condition is met, calls close_fn.
  4. Reverts EXITING positions stuck > 5 minutes back to OPEN.

No imports from server.py. Dependencies are injected via run_monitor_loop().
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Dict, Optional

from core.position_lifecycle import (
    exit_reason,
    normalize_strategy_risk,
    position_risk_prices,
)

logger = logging.getLogger("quantg.position_monitor")

# ── IST helpers ────────────────────────────────────────────────────────────────

def _in_market_hours() -> bool:
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:
        return False
    minutes = ist.hour * 60 + ist.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


# ── Main loop ──────────────────────────────────────────────────────────────────

async def run_monitor_loop(
    db,
    stop_event: asyncio.Event,
    *,
    close_fn: Callable[..., Coroutine],
    quote_ltp_fn: Callable[..., Coroutine],
    get_ltp_fn: Callable[..., Coroutine],
    get_settings_fn: Callable[..., Coroutine],
) -> None:
    """
    close_fn(user_id, strategy_id, reason)  → closes all positions for the strategy
    quote_ltp_fn(user_id, instrument_key)   → float | None from V3 feed cache
    get_ltp_fn(user_id, symbol, exchange, allow_mock, execution_broker) → float | None
    get_settings_fn(user_id)                → dict of user settings
    """
    logger.info("Position monitor started")
    while not stop_event.is_set():
        try:
            await _monitor_tick(db, close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn)
        except Exception as exc:
            logger.warning("Position monitor tick error: %s", exc)
        # 30-second sleep broken into 1-second slices so stop_event is responsive
        slept = 0
        while not stop_event.is_set() and slept < 30:
            await asyncio.sleep(1)
            slept += 1
    logger.info("Position monitor stopped")


async def _monitor_tick(db, close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn) -> None:
    in_hours = _in_market_hours()

    # ── Revert positions stuck in EXITING > 5 minutes ─────────────────────────
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    stuck = await db.strategy_positions.find(
        {"status": "EXITING", "updated_at": {"$lt": stale_cutoff}},
        {"id": 1, "user_id": 1, "_id": 0},
    ).to_list(100)
    for sp in stuck:
        result = await db.strategy_positions.update_one(
            {"id": sp["id"], "status": "EXITING"},
            {"$set": {"status": "OPEN", "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
        if result.modified_count:
            logger.warning("Reverted stuck EXITING position id=%s to OPEN for retry", sp["id"])

    # ── Process each open position ─────────────────────────────────────────────
    rows = await db.strategy_positions.find(
        {"status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}},
        {"_id": 0},
    ).to_list(1000)

    for pos in rows:
        user_id = pos.get("user_id")
        sid = pos.get("strategy_id")
        symbol = pos.get("target_symbol") or pos.get("trading_symbol") or pos.get("symbol")
        if not user_id or not sid or not symbol:
            continue

        # ── Fetch LTP ──────────────────────────────────────────────────────────
        ltp = await quote_ltp_fn(user_id, pos.get("instrument_key") or pos.get("instrument_token"))
        if ltp is None:
            settings = await get_settings_fn(user_id)
            is_paper = pos.get("mode") == "paper"
            allow_sim = bool(settings.get("allow_simulated_prices")) or \
                        os.environ.get("QUANTG_ALLOW_SIMULATED_PRICES", "").lower() == "true"
            ltp = await get_ltp_fn(
                user_id, symbol,
                pos.get("exchange") or "NSE",
                allow_mock=is_paper and allow_sim,
                execution_broker="upstox",
            )

        # ── Fallback: paper_quote_cache for paper positions (WS may not be subscribed to options) ──
        if ltp is None and pos.get("mode") == "paper":
            ikey = pos.get("instrument_key") or pos.get("cache_key") or pos.get("subscribed_key")
            if not ikey:
                # Derive instrument_key from trading_symbol via instruments collection
                trading_sym = pos.get("trading_symbol") or pos.get("target_symbol")
                if trading_sym:
                    inst_doc = await db.upstox_instruments.find_one(
                        {"tradingsymbol": trading_sym}, {"instrument_key": 1, "_id": 0}
                    )
                    if inst_doc:
                        ikey = inst_doc.get("instrument_key")
            if ikey:
                cache_doc = await db.paper_quote_cache.find_one({"instrument_key": ikey})
                if cache_doc and cache_doc.get("ltp") is not None:
                    ltp = float(cache_doc["ltp"])
                    logger.debug("position_monitor: LTP %.2f from paper_quote_cache for %s", ltp, ikey)

        if ltp is None:
            await db.strategy_positions.update_one(
                {"id": pos["id"], "user_id": user_id},
                {"$set": {
                    "last_ltp": "LTP_UNAVAILABLE",
                    "last_error": "LTP_UNAVAILABLE: feed offline or instrument not subscribed",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
            continue

        # ── Update LTP, P&L, and risk prices ──────────────────────────────────
        ltp = float(ltp)
        entry = float(pos.get("average_buy_price") or 0)
        qty = int(pos.get("open_quantity") or pos.get("quantity") or 0)
        side = str(pos.get("position_side") or "LONG").upper()
        pnl = round((entry - ltp) * qty, 2) if side == "SHORT" else round((ltp - entry) * qty, 2)

        risk_prices = position_risk_prices(pos, ltp=ltp)
        risk = normalize_strategy_risk(pos.get("tp_sl_tsl_config") or {})
        if risk_prices.get("stop_loss") is not None:
            risk["stoploss_price"] = risk_prices["stop_loss"]
            risk["stop_loss"] = risk_prices["stop_loss"]
        if risk_prices.get("take_profit") is not None:
            risk["target_price"] = risk_prices["take_profit"]
            risk["take_profit"] = risk_prices["take_profit"]
        if risk_prices.get("trailing_sl") is not None:
            risk["trailing_sl"] = risk_prices["trailing_sl"]

        now_str = datetime.now(timezone.utc).isoformat()
        await db.strategy_positions.update_one(
            {"id": pos["id"], "user_id": user_id},
            {
                "$set": {
                    "last_ltp": ltp,
                    "unrealized_pnl": pnl,
                    "last_tick_at": now_str,
                    "updated_at": now_str,
                    "tp_sl_tsl_config": risk,
                },
                "$unset": {"last_error": ""},
            },
        )

        # ── Exit check (only during market hours) ─────────────────────────────
        if not in_hours:
            continue
        reason = exit_reason(pos, ltp)
        if reason:
            logger.info("Monitor exit strategy=%s symbol=%s reason=%s ltp=%.2f", sid, symbol, reason, ltp)
            await close_fn(user_id, sid, reason=reason)
