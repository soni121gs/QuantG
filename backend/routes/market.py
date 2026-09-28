from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

import options_helper
from core import db, get_current_user

router = APIRouter(tags=["Market"])


@router.get("/market/watchlist")
async def watchlist(user=Depends(get_current_user)):
    from server import INDEX_WATCHLIST, _upstox_watchlist_rows

    return await _upstox_watchlist_rows(user["id"])


@router.get("/market/iv-rank")
async def market_iv_rank(user=Depends(get_current_user)):
    from iv_regime import (
        compute_iv_rank,
        IV_RANK_GATE_ENABLED,
        IV_RANK_GATE_SHADOW,
        IV_RANK_BUY_MAX,
    )

    data = await compute_iv_rank(db)
    if not data:
        return {"available": False, "reason": "insufficient India VIX history"}
    iv_rank = data.get("iv_rank")
    would_block = iv_rank is not None and iv_rank > IV_RANK_BUY_MAX
    gate_state = "enabled" if IV_RANK_GATE_ENABLED else ("shadow" if IV_RANK_GATE_SHADOW else "off")
    return {
        "available": True,
        **data,
        "buy_max": IV_RANK_BUY_MAX,
        "gate_state": gate_state,
        "would_block_buys": bool(would_block),
        "blocking": bool(would_block and IV_RANK_GATE_ENABLED),
    }


@router.get("/market/candles/{instrument_key:path}")
async def market_candles(
    instrument_key: str,
    interval: str = "1day",
    from_date: str | None = None,
    to_date: str | None = None,
    user=Depends(get_current_user),
):
    from server import _analytics

    if not _analytics:
        raise HTTPException(status_code=503, detail="Analytics Token not configured on server.")
    candles = await _analytics.get_historical_candles(instrument_key, interval, from_date, to_date)
    return {"instrument_key": instrument_key, "interval": interval, "candles": candles, "count": len(candles)}


@router.get("/market/analytics/option-chain")
async def analytics_option_chain(
    instrument_key: str,
    expiry_date: str,
    user=Depends(get_current_user),
):
    from server import _analytics

    if not _analytics:
        raise HTTPException(status_code=503, detail="Analytics Token not configured on server.")
    data = await _analytics.get_option_chain(instrument_key, expiry_date)
    return data


@router.get("/market/analytics/expiry-dates")
async def analytics_expiry_dates(
    instrument_key: str,
    user=Depends(get_current_user),
):
    from server import _analytics

    if not _analytics:
        raise HTTPException(status_code=503, detail="Analytics Token not configured on server.")
    dates = await _analytics.get_option_expiry_dates(instrument_key)
    return {"instrument_key": instrument_key, "expiry_dates": dates}


@router.get("/market/commodities")
async def commodity_watchlist(user=Depends(get_current_user)):
    raise HTTPException(status_code=410, detail="MCX commodity systems have been removed. QuantG is Upstox-only for NSE/BSE/NFO/BFO.")


@router.get("/market/quote/{symbol}")
async def quote(symbol: str, user=Depends(get_current_user)):
    from server import REMOVED_COMMODITY_UNDERLYINGS, INDEX_WATCHLIST, _upstox_watchlist_rows

    if symbol.upper() in REMOVED_COMMODITY_UNDERLYINGS:
        raise HTTPException(status_code=410, detail="MCX commodity symbols have been removed from QuantG.")
    found = next((srow for srow in INDEX_WATCHLIST if srow["symbol"] == symbol.upper()), None)
    if not found:
        raise HTTPException(status_code=404, detail="Symbol not found")
    row = next((r for r in await _upstox_watchlist_rows(user["id"]) if r["symbol"] == found["symbol"]), None)
    if not row or row.get("price") is None:
        raise HTTPException(status_code=503, detail="Verified Upstox quote unavailable; no fallback price is shown.")
    return {**row, "verified": True, "quote_timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("/options/preview")
async def options_preview(
    underlying: str = "NIFTY",
    strike_mode: str = "ATM_BUY",
    otm_points: int = 0,
    action: str = "BUY",
    expiry_offset: int = 0,
    user=Depends(get_current_user),
):
    if underlying.upper() not in options_helper.SUPPORTED:
        raise HTTPException(status_code=400, detail=f"Underlying must be one of {options_helper.SUPPORTED}")
    return {
        "available": False,
        "reason": "Live contract preview uses Upstox option-chain resolution during execution. This screen shows static config only.",
        "underlying": underlying.upper(),
        "lot_size": options_helper.LOT_SIZES.get(underlying.upper()),
        "strike_interval": options_helper.STRIKE_INTERVALS.get(underlying.upper()),
        "exchange": options_helper.OPT_EXCHANGE.get(underlying.upper()),
        "broker": "upstox",
    }


@router.get("/market/feed-comparison")
async def market_feed_comparison(user=Depends(get_current_user)):
    from server import _age_ms, get_user_settings, get_user_upstox_status

    settings = await get_user_settings(user["id"])
    upstox = await get_user_upstox_status(user["id"])
    gateway = upstox.get("gateway") or {}
    last_tick = gateway.get("last_tick_at")
    age = _age_ms(last_tick)
    healthy = bool(upstox.get("connected") and (gateway.get("ticks", 0) > 0 or gateway.get("feed_status", {}).get("connected")))
    return {
        "configured_data_broker": "upstox",
        "recommended_data_broker": "upstox",
        "reason": "QuantG is configured for Upstox-only market data.",
        "price_provider_chain": [
            "Upstox websocket LTP",
            "Upstox REST quote fallback",
            "historical candle close only for backtest/simulation profiles",
            "no price",
        ],
        "simulated_feed_allowed": bool(settings.get("allow_simulated_prices")) or os.environ.get("QUANTG_ALLOW_SIMULATED_PRICES", "").lower() == "true",
        "simulated_warning": "Simulated feed active - paper results are not market-valid." if bool(settings.get("allow_simulated_prices")) else None,
        "brokers": {
            "upstox": {
                "connected": bool(upstox.get("connected")),
                "authenticated": bool(upstox.get("authenticated")),
                "last_tick_at": last_tick,
                "age_ms": age,
                "subscribed_tokens": gateway.get("subscribed_tokens", 0),
                "ticks": gateway.get("ticks", 0),
                "last_error": gateway.get("last_error") or upstox.get("reason"),
                "healthy": healthy,
            }
        },
        "upstox": {
            "connected": bool(upstox.get("connected")),
            "authenticated": bool(upstox.get("authenticated")),
            "last_tick_at": last_tick,
            "age_ms": age,
            "healthy": healthy,
        },
    }


@router.get("/market/nifty-live")
async def market_nifty_live(user=Depends(get_current_user)):
    """Read-only NIFTY spot snapshot and recent 5-minute candles."""
    from server import get_user_upstox_gateway

    key = "NSE_INDEX|Nifty 50"
    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway:
        return {"available": False, "source": "none", "reason": "Upstox gateway unavailable", "instrument_key": key}

    tick = (gateway.latest_ticks() or {}).get(key) or {}
    candles = []
    try:
        candles = await asyncio.to_thread(gateway.get_historical_candles, key, "5minute", 1) or []
    except Exception:
        candles = []
    ltp = tick.get("ltp")
    if ltp is None and candles:
        ltp = candles[-1].get("close")
    return {
        "available": ltp is not None,
        "instrument_key": key,
        "ltp": round(float(ltp), 2) if ltp is not None else None,
        "previous_close": tick.get("prev_close_price"),
        "tick_time": tick.get("timestamp") or tick.get("received_at"),
        "received_at": tick.get("received_at"),
        "source": tick.get("feed") or tick.get("source") or ("historical" if candles else "none"),
        "candles": candles[-78:],
        "candle_interval": "5minute",
        "is_live": bool(tick.get("ltp") is not None and tick.get("feed") == "upstox-v3"),
        "note": "Read-only market display; it does not place or alter orders.",
    }


@router.get("/market/indices-live")
async def market_indices_live(user=Depends(get_current_user)):
    """Read-only live snapshots and recent candles for the three index anchors."""
    from server import get_user_upstox_gateway

    instruments = {
        "NIFTY": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "SENSEX": "BSE_INDEX|SENSEX",
    }
    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway:
        return {"available": False, "indices": {}, "reason": "Upstox gateway unavailable"}
    ticks = gateway.latest_ticks() or {}
    indices = {}
    for name, key in instruments.items():
        tick = ticks.get(key) or {}
        try:
            candles = await asyncio.to_thread(gateway.get_historical_candles, key, "5minute", 1) or []
        except Exception:
            candles = []
        ltp = tick.get("ltp")
        if ltp is None and candles:
            ltp = candles[-1].get("close")
        indices[name] = {
            "instrument_key": key,
            "ltp": round(float(ltp), 2) if ltp is not None else None,
            "tick_time": tick.get("timestamp") or tick.get("received_at"),
            "received_at": tick.get("received_at"),
            "source": tick.get("feed") or tick.get("source") or ("historical" if candles else "none"),
            "candles": candles[-78:],
            "is_live": bool(tick.get("ltp") is not None and tick.get("feed") == "upstox-v3"),
        }
    return {"available": any(row["ltp"] is not None for row in indices.values()), "indices": indices, "interval": "5minute", "note": "Read-only market display; it does not place or alter orders."}


@router.post("/market/auto-data-broker")
async def market_auto_data_broker(user=Depends(get_current_user)):
    comparison = await market_feed_comparison(user=user)
    await db.users.update_one({"id": user["id"]}, {"$set": {"data_broker": "upstox", "execution_broker": "upstox", "fallback_broker": "none"}})
    comparison["updated"] = True
    comparison["configured_data_broker"] = "upstox"
    return comparison


@router.get("/market/indicators/{symbol}")
async def market_indicators(symbol: str, user=Depends(get_current_user)):
    from server import MarketTrendAnalyzer, _fetch_strategy_history

    symbol = symbol.upper()
    history = await _fetch_strategy_history(user["id"], symbol, days=60, interval="5minute")
    data = history.get("data") or []
    if len(data) < 20:
        return {
            "symbol": symbol,
            "source": history.get("source", "none"),
            "is_live": bool(history.get("is_live")),
            "available": False,
            "reason": "Not enough candles yet for indicator stack.",
        }
    trend = MarketTrendAnalyzer.analyze(data, lookback=min(80, max(20, len(data))))
    validation_context = {
        "trend": trend.get("trend"),
        "strength": trend.get("strength"),
        "rsi": trend.get("rsi"),
        "atr_pct": trend.get("atr_pct"),
        "vwap_distance_pct": trend.get("vwap_distance_pct"),
        "higher_timeframe": trend.get("higher_timeframe"),
        "volume_ratio": trend.get("volume_ratio"),
        "support": trend.get("support"),
        "resistance": trend.get("resistance"),
    }
    return {
        "symbol": symbol,
        "source": history.get("source", "unknown"),
        "is_live": bool(history.get("is_live")),
        "paper_mode": bool(history.get("paper_mode")),
        "available": True,
        "candles": len(data),
        "last_candle": data[-1],
        "indicators": validation_context,
    }


@router.get("/market/session-status")
async def market_session_status():
    from core.market_clock import get_market_clock_snapshot

    return get_market_clock_snapshot()


@router.get("/market/session")
async def market_session():
    from core.market_clock import get_market_clock_snapshot
    from server import IST_OFFSET, NSE_CLOSE_MINUTE

    snapshot = get_market_clock_snapshot()
    nse = snapshot["segments"]["NSE_FO"]
    now_ist = datetime.now(timezone.utc) + IST_OFFSET
    minutes = now_ist.hour * 60 + now_ist.minute
    return {
        **snapshot,
        "market": "NSE",
        "server_time_ist": snapshot["current_ist_time"],
        "open": nse["open"],
        "status": nse["status"],
        "open_time": nse["open_time"],
        "close_time": nse["close_time"],
        "minutes_to_close": max(0, NSE_CLOSE_MINUTE - minutes) if nse["open"] else None,
    }


@router.get("/option-chain/{underlying}")
async def option_chain(underlying: str, width: int = 5, user=Depends(get_current_user)):
    from server import get_user_upstox_gateway

    underlying = underlying.upper()
    if underlying not in options_helper.SUPPORTED:
        raise HTTPException(status_code=400, detail=f"Underlying must be one of {options_helper.SUPPORTED}")
    gw = await get_user_upstox_gateway(user["id"])
    spot_key = {
        "NIFTY": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "SENSEX": "BSE_INDEX|SENSEX",
    }.get(underlying)
    if not gw or not gw.connected or not spot_key:
        raise HTTPException(status_code=503, detail="Verified Upstox option-chain data unavailable; no synthetic prices are shown.")
    raise HTTPException(
        status_code=410,
        detail="Synthetic option-chain preview removed. Use /api/upstox/option-chain for broker quotes.",
    )


@router.get("/market/regime")
async def market_regime_status(user=Depends(get_current_user)):
    from market_regime import get_all_regimes

    try:
        db_regimes = await db.market_regime_state.find({}, {"_id": 0}).to_list(10)
        db_map = {r["index"]: r for r in db_regimes}
    except Exception:
        db_map = {}
    mem = get_all_regimes()
    result = {}
    for idx in ("NIFTY", "BANKNIFTY", "SENSEX"):
        result[idx] = mem.get(idx) or db_map.get(idx) or {"index": idx, "regime": "UNKNOWN"}
    return {"regimes": result, "as_of": datetime.now(timezone.utc).isoformat()}
