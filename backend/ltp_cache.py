"""Shared per-instrument LTP cache with configurable TTL.

Both position_monitor and position_guardian poll at 5-second intervals.
Without this cache, two concurrent REST quote calls per instrument per tick
hit Upstox rate limits on fallback days. The cache de-duplicates those calls.

Usage:
    from ltp_cache import get_cached_ltp
    ltp = await get_cached_ltp(
        instrument_key,
        _quote_upstox_instrument_key, user_id, instrument_key
    )
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, Coroutine, Dict, Optional, Tuple

logger = logging.getLogger("quantg.ltp_cache")

LTP_CACHE_TTL_SECONDS: float = float(os.environ.get("LTP_CACHE_TTL_SECONDS", "2.5"))

_cache: Dict[str, Tuple[Optional[float], float]] = {}
_inflight: Dict[str, asyncio.Future] = {}
_lock = asyncio.Lock()


async def get_cached_ltp(
    cache_key: str,
    fetch_fn: Callable[..., Coroutine[Any, Any, Optional[float]]],
    *fetch_args,
    ttl: float = LTP_CACHE_TTL_SECONDS,
) -> Optional[float]:
    """Return a cached LTP or fetch fresh. Concurrent callers for the same key
    coalesce onto one fetch — at most one REST call per TTL window per key."""
    now = time.monotonic()

    # Fast-path: cache hit without the lock
    entry = _cache.get(cache_key)
    if entry is not None and now - entry[1] < ttl:
        return entry[0]

    async with _lock:
        # Re-check under the lock
        entry = _cache.get(cache_key)
        if entry is not None and now - entry[1] < ttl:
            return entry[0]

        # Coalesce: wait for in-flight fetch if one is running
        if cache_key in _inflight:
            fut = _inflight[cache_key]
            try:
                return await asyncio.shield(fut)
            except Exception:
                return None

        # No cache, no in-flight: create a future others can coalesce on
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        _inflight[cache_key] = fut

    try:
        ltp = await fetch_fn(*fetch_args)
        _cache[cache_key] = (ltp, time.monotonic())
        if not fut.done():
            fut.set_result(ltp)
        return ltp
    except Exception as exc:
        logger.debug("ltp_cache: fetch failed key=%s: %s", cache_key, exc)
        _cache[cache_key] = (None, time.monotonic())
        if not fut.done():
            fut.set_result(None)
        return None
    finally:
        _inflight.pop(cache_key, None)


def invalidate(cache_key: str) -> None:
    _cache.pop(cache_key, None)


def clear_all() -> None:
    _cache.clear()
