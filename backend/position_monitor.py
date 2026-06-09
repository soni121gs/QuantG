"""Position monitor loop — extracted from server.py.

Runs every 30 seconds. For every OPEN/FILLED/PENDING_BROKER position:
  1. Fetches live LTP from Upstox V3 feed (or falls back through chain).
  2. Updates last_ltp, unrealized_pnl, ltp_source, and risk prices in DB.
  3. If in IST market hours AND exit condition is met, calls close_fn.
  4. Reverts EXITING positions stuck > 5 minutes back to OPEN.

LTP source chain (FIX 2):
  WS_CACHE       — V3 websocket tick (freshest)
  REST_FALLBACK  — Upstox REST quote API via quote_ltp_fn
  SYMBOL_LTP     — symbol+exchange lookup via get_ltp_fn
  PAPER_CACHE    — db.paper_quote_cache (REST snapshot stored earlier)
  ENTRY_PRICE_FALLBACK — position's own average_buy_price (last resort, safe)

Per-position try/except (FIX 9): a crash on one position never stops the loop.

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

# LTP source labels used in DB and logs
LTP_WS_CACHE            = "WS_CACHE"
LTP_REST_FALLBACK       = "REST_FALLBACK"
LTP_SYMBOL              = "SYMBOL_LTP"
LTP_PAPER_CACHE         = "PAPER_CACHE"
LTP_ENTRY_PRICE         = "ENTRY_PRICE_FALLBACK"


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
    quote_ltp_fn(user_id, instrument_key)   → float | None  (V3 feed cache → REST)
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

    # ── Process each open position (FIX 9: per-position try/except) ───────────
    rows = await db.strategy_positions.find(
        {"status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}},
        {"_id": 0},
    ).to_list(1000)

    for pos in rows:
        # FIX 9: isolate each position — a crash on one never stops the rest
        try:
            await _process_one_position(
                db, pos, in_hours, close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn
            )
        except Exception as pos_exc:
            logger.error(
                "position_monitor: unhandled error on pos=%s user=%s symbol=%s: %s",
                pos.get("id"), pos.get("user_id"), pos.get("target_symbol"), pos_exc,
            )


async def _resolve_ltp(
    db,
    pos: Dict[str, Any],
    quote_ltp_fn,
    get_ltp_fn,
    get_settings_fn,
) -> tuple[Optional[float], str]:
    """
    FIX 2: Full LTP resolution chain with source tracking.

    Returns (ltp, source) where source is one of the LTP_* constants.
    NEVER returns None — falls back to entry_price if all live sources fail.
    """
    user_id = pos.get("user_id")
    symbol  = pos.get("target_symbol") or pos.get("trading_symbol") or pos.get("symbol")
    ikey    = pos.get("instrument_key") or pos.get("instrument_token")

    # ── Source 1: V3 WS cache → REST (via quote_ltp_fn) ──────────────────────
    # quote_ltp_fn already tries WS tick first, then REST get_market_quote.
    if ikey:
        ltp = await quote_ltp_fn(user_id, ikey)
        if ltp is not None:
            return float(ltp), LTP_WS_CACHE  # covers both WS and REST in one call

    # ── Source 2: Symbol-based get_ltp ────────────────────────────────────────
    try:
        settings = await get_settings_fn(user_id)
        is_paper = pos.get("mode") == "paper"
        allow_sim = (
            bool(settings.get("allow_simulated_prices"))
            or os.environ.get("QUANTG_ALLOW_SIMULATED_PRICES", "").lower() == "true"
        )
        ltp = await get_ltp_fn(
            user_id, symbol,
            pos.get("exchange") or "NSE",
            allow_mock=is_paper and allow_sim,
            execution_broker="upstox",
        )
        if ltp is not None:
            return float(ltp), LTP_SYMBOL
    except Exception as e:
        logger.debug("position_monitor: get_ltp_fn failed for %s: %s", symbol, e)

    # ── Source 3: paper_quote_cache (REST snapshot keyed by instrument_key) ───
    if pos.get("mode") == "paper":
        cache_key = ikey
        if not cache_key:
            trading_sym = pos.get("trading_symbol") or pos.get("target_symbol")
            if trading_sym:
                inst_doc = await db.upstox_instruments.find_one(
                    {"tradingsymbol": trading_sym}, {"instrument_key": 1, "_id": 0}
                )
                if inst_doc:
                    cache_key = inst_doc.get("instrument_key")
        if cache_key:
            cache_doc = await db.paper_quote_cache.find_one({"instrument_key": cache_key})
            if cache_doc and cache_doc.get("ltp") is not None:
                try:
                    ltp = float(cache_doc["ltp"])
                    return ltp, LTP_PAPER_CACHE
                except (TypeError, ValueError):
                    pass

    # ── Source 4: Entry price fallback (safe last resort) ─────────────────────
    # Using entry price means no false SL/TP triggers (pnl = 0 when ltp == entry).
    # The exit logic downstream knows to use MARKET order when ltp_source is this.
    entry_price = float(
        pos.get("average_buy_price") or pos.get("average_price") or pos.get("avg_price") or 0
    )
    if entry_price > 0:
        logger.warning(
            "position_monitor: LTP unavailable for %s pos=%s ikey=%s — "
            "using ENTRY_PRICE_FALLBACK=%.2f. Exit will use MARKET order.",
            symbol, pos.get("id"), ikey, entry_price,
        )
        return entry_price, LTP_ENTRY_PRICE

    return None, "NONE"


async def _process_one_position(
    db, pos, in_hours, close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn
) -> None:
    user_id = pos.get("user_id")
    sid     = pos.get("strategy_id")
    symbol  = pos.get("target_symbol") or pos.get("trading_symbol") or pos.get("symbol")
    if not user_id or not sid or not symbol:
        return

    # ── Fetch LTP with full fallback chain ────────────────────────────────────
    ltp, ltp_source = await _resolve_ltp(db, pos, quote_ltp_fn, get_ltp_fn, get_settings_fn)

    if ltp is None:
        # All sources exhausted — mark unavailable and skip exit check
        await db.strategy_positions.update_one(
            {"id": pos["id"], "user_id": user_id},
            {"$set": {
                "last_ltp": "LTP_UNAVAILABLE",
                "ltp_source": "NONE",
                "last_error": "LTP_UNAVAILABLE: all sources exhausted (WS/REST/cache/entry_price)",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
        )
        logger.warning(
            "position_monitor: all LTP sources exhausted pos=%s user=%s symbol=%s — no exit check",
            pos["id"], user_id, symbol,
        )
        return

    # ── Update LTP, P&L, and risk prices ──────────────────────────────────────
    ltp   = float(ltp)
    entry = float(pos.get("average_buy_price") or 0)
    qty   = int(pos.get("open_quantity") or pos.get("quantity") or 0)
    side  = str(pos.get("position_side") or "LONG").upper()
    pnl   = round((entry - ltp) * qty, 2) if side == "SHORT" else round((ltp - entry) * qty, 2)

    risk_prices = position_risk_prices(pos, ltp=ltp)
    risk = normalize_strategy_risk(pos.get("tp_sl_tsl_config") or {})
    if risk_prices.get("stop_loss") is not None:
        risk["stoploss_price"] = risk_prices["stop_loss"]
        risk["stop_loss"]      = risk_prices["stop_loss"]
    if risk_prices.get("take_profit") is not None:
        risk["target_price"] = risk_prices["take_profit"]
        risk["take_profit"]  = risk_prices["take_profit"]
    if risk_prices.get("trailing_sl") is not None:
        risk["trailing_sl"] = risk_prices["trailing_sl"]

    now_str = datetime.now(timezone.utc).isoformat()
    await db.strategy_positions.update_one(
        {"id": pos["id"], "user_id": user_id},
        {
            "$set": {
                "last_ltp":       ltp,
                "ltp_source":     ltp_source,
                "unrealized_pnl": pnl,
                "last_tick_at":   now_str,
                "updated_at":     now_str,
                "tp_sl_tsl_config": risk,
            },
            "$unset": {"last_error": ""},
        },
    )

    # ── Exit check (only during market hours) ─────────────────────────────────
    if not in_hours:
        return

    # Don't trigger SL/TP exits using entry-price fallback — P&L is always 0
    # so no real exit condition can fire. Time exits are still valid.
    if ltp_source == LTP_ENTRY_PRICE:
        reason = exit_reason(pos, ltp)
        if reason and "TIME" not in reason.upper():
            # SL/TP exit with stale price — skip to avoid false trigger
            logger.debug(
                "position_monitor: skipping %s exit for %s (ltp_source=ENTRY_PRICE_FALLBACK)",
                reason, symbol,
            )
            return

    reason = exit_reason(pos, ltp)
    if reason:
        logger.info(
            "Monitor exit strategy=%s symbol=%s reason=%s ltp=%.2f source=%s",
            sid, symbol, reason, ltp, ltp_source,
        )
        await close_fn(user_id, sid, reason=reason, ltp_source=ltp_source)
