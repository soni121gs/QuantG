"""Real underlying OHLC store for the options backtester.

The chain snapshots in db.historical_chains carry only a flat `spot` per ~5-min
bar (open == high == low == close), so breakout / range / VWAP signals never fire
in a backtest — every "candle" is a dot. This module backfills REAL 5-min
underlying OHLC (via the Upstox V3 historical-candle REST path, which DOES work
for index keys) into db.candles, keyed by IST minute so it lines up with the
chain snapshots that price the option legs.

Division of labour (TASK-052): db.candles drives SIGNALS (real high/low/close);
db.historical_chains stays the source of truth for option PRICING.

db.candles doc:
    {underlying, interval, date "YYYY-MM-DD", minute "YYYY-MM-DD HH:MM",
     open, high, low, close, volume}
Unique on (underlying, interval, minute).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from pymongo import UpdateOne

logger = logging.getLogger("quantg.candle_store")

# Index instrument keys for the underlyings we trade. The V3 historical-candle
# endpoint accepts these index keys (unlike the V2 intraday path).
UNDERLYING_INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|Nifty Midcap Select",
    "SENSEX": "BSE_INDEX|SENSEX",
}

DEFAULT_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"]


async def ensure_indexes(db) -> None:
    try:
        await db.candles.create_index(
            [("underlying", 1), ("interval", 1), ("minute", 1)],
            unique=True, name="uniq_underlying_interval_minute",
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("candles index create skipped: %s", exc)


async def backfill_underlying_candles(
    db,
    gateway,
    underlyings: Optional[List[str]] = None,
    days: int = 30,
    interval: int = 5,
) -> Dict[str, Any]:
    """Fetch real OHLC for each underlying over the last `days` and upsert into
    db.candles. Returns a per-underlying summary. Requires a connected gateway.

    Upstox caps minute-granularity history per request (~1 month for 5-min), so
    `days` defaults to 30. Idempotent: re-running overwrites the same bars.
    """
    await ensure_indexes(db)
    if underlyings is None:
        underlyings = list(DEFAULT_UNDERLYINGS)
    end = datetime.now()
    start = end - timedelta(days=days)
    out: Dict[str, Any] = {}

    for u in underlyings:
        uk = u.upper()
        key = UNDERLYING_INDEX_KEYS.get(uk)
        if not key:
            out[uk] = {"error": "no index instrument key"}
            continue
        try:
            bars = gateway.get_historical_candles_v3(
                key, unit="minutes", interval=interval,
                from_date=start.strftime("%Y-%m-%d"),
                to_date=end.strftime("%Y-%m-%d"),
            ) or []
        except Exception as exc:  # noqa: BLE001
            out[uk] = {"error": str(exc)[:160]}
            continue
        # The historical path lags today's session; pull today's intraday bars too
        # so the priceable backtest window includes the current day's chains.
        try:
            intraday = gateway.get_intraday_candles_v3(key, unit="minutes", interval=interval) or []
            if intraday:
                seen = {str(b.get("date"))[:16] for b in bars}
                bars = bars + [b for b in intraday if str(b.get("date"))[:16] not in seen]
                bars.sort(key=lambda b: str(b.get("date")))
        except Exception as exc:  # noqa: BLE001
            logger.debug("intraday candle fetch failed for %s: %s", uk, exc)

        ops: List[UpdateOne] = []
        for b in bars:
            minute = str(b.get("date") or "")[:16]
            if len(minute) < 16:
                continue
            ops.append(UpdateOne(
                {"underlying": uk, "interval": interval, "minute": minute},
                {"$set": {
                    "underlying": uk, "interval": interval,
                    "minute": minute, "date": minute[:10],
                    "open": float(b.get("open") or 0), "high": float(b.get("high") or 0),
                    "low": float(b.get("low") or 0), "close": float(b.get("close") or 0),
                    "volume": int(b.get("volume") or 0),
                }},
                upsert=True,
            ))
        if ops:
            res = await db.candles.bulk_write(ops, ordered=False)
            n_up = (res.upserted_count or 0) + (res.modified_count or 0)
        else:
            n_up = 0
        span = (bars[0]["date"], bars[-1]["date"]) if bars else None
        out[uk] = {"fetched": len(bars), "written": n_up, "span": span}
        logger.info("candle backfill %s: fetched=%d written=%d span=%s", uk, len(bars), n_up, span)
    return out


async def load_candles(
    db,
    underlying: str,
    interval: int = 5,
    start_minute: Optional[str] = None,
    end_minute: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load stored OHLC for one underlying, oldest-first, in the standard
    {date, open, high, low, close, volume} shape strategy code expects."""
    q: Dict[str, Any] = {"underlying": underlying.upper(), "interval": interval}
    if start_minute or end_minute:
        rng: Dict[str, Any] = {}
        if start_minute:
            rng["$gte"] = start_minute
        if end_minute:
            rng["$lte"] = end_minute
        q["minute"] = rng
    docs = await db.candles.find(q).sort("minute", 1).to_list(length=500_000)
    return [{
        "date": d["minute"], "open": d.get("open", 0.0), "high": d.get("high", 0.0),
        "low": d.get("low", 0.0), "close": d.get("close", 0.0), "volume": d.get("volume", 0),
    } for d in docs]
