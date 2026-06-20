"""Shared position LTP resolver.

Centralizes the fallback resolution of Last Traded Price (LTP) across monitor
and guardian loops, respecting cash-equity WS cache rules and fallback options.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Coroutine, Dict, Optional

from ltp_cache import get_cached_ltp as _get_cached_ltp

logger = logging.getLogger("quantg.ltp_resolver")


def should_trust_ws_cache(instrument_key: str | None) -> bool:
    """
    Returns True if instrument_key is not None.
    """
    return bool(instrument_key)


async def resolve_position_ltp(
    db,
    pos: Dict[str, Any],
    quote_ltp_fn: Callable[..., Coroutine[Any, Any, Optional[float]]],
    get_ltp_fn: Callable[..., Coroutine[Any, Any, Optional[float]]],
    get_settings_fn: Callable[..., Coroutine[Any, Any, Dict[str, Any]]],
    *,
    allow_entry_fallback: bool,
) -> tuple[Optional[float], str]:
    """
    LTP resolution chain with source tracking.
    
    Returns (ltp, source) where source is one of the LTP_* constants.
    """
    user_id = pos.get("user_id")
    symbol  = pos.get("target_symbol") or pos.get("trading_symbol") or pos.get("symbol")
    ikey    = pos.get("instrument_key") or pos.get("instrument_token")

    # ── Source 1: V3 WS cache → REST (via quote_ltp_fn), with shared TTL cache ─
    if should_trust_ws_cache(ikey):
        cache_key = f"ltp:{user_id}:{ikey}"
        try:
            ltp = await _get_cached_ltp(cache_key, quote_ltp_fn, user_id, ikey)
            if ltp is not None:
                return float(ltp), "WS_CACHE"
        except Exception:
            pass

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
            return float(ltp), "SYMBOL_LTP"
    except Exception as e:
        logger.debug("ltp_resolver: get_ltp_fn failed for %s: %s", symbol, e)

    # ── Source 3: paper_quote_cache (REST snapshot keyed by instrument_key) ───
    if pos.get("mode") == "paper":
        cache_key = ikey
        if not cache_key:
            trading_sym = pos.get("trading_symbol") or pos.get("target_symbol")
            if trading_sym:
                try:
                    inst_doc = await db.upstox_instruments.find_one(
                        {"tradingsymbol": trading_sym}, {"instrument_key": 1, "_id": 0}
                    )
                    if inst_doc:
                        cache_key = inst_doc.get("instrument_key")
                except Exception:
                    pass
        if cache_key:
            try:
                cache_doc = await db.paper_quote_cache.find_one({"instrument_key": cache_key})
                if cache_doc and cache_doc.get("ltp") is not None:
                    return float(cache_doc["ltp"]), "PAPER_CACHE"
            except Exception:
                pass

    # ── Source 4: Entry price fallback (safe last resort) ─────────────────────
    if allow_entry_fallback:
        entry_price = float(
            pos.get("average_buy_price") or pos.get("average_price") or pos.get("avg_price") or 0
        )
        if entry_price > 0:
            logger.warning(
                "ltp_resolver: LTP unavailable for %s pos=%s ikey=%s — "
                "using ENTRY_PRICE_FALLBACK=%.2f. Exit will use MARKET order.",
                symbol, pos.get("id"), ikey, entry_price,
            )
            return entry_price, "ENTRY_PRICE_FALLBACK"

    return None, "NONE"
