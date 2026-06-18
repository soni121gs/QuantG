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

    upstox_rows = await _upstox_watchlist_rows(user["id"])
    if upstox_rows:
        return upstox_rows
    return [
        {"symbol": s["symbol"], "name": s["name"], "price": s["base"], "change": 0.0, "pct": 0.0, "source": "fallback"}
        for s in INDEX_WATCHLIST
    ]


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
    from server import REMOVED_COMMODITY_UNDERLYINGS, SYMBOLS, historical_series, live_price

    if symbol.upper() in REMOVED_COMMODITY_UNDERLYINGS:
        raise HTTPException(status_code=410, detail="MCX commodity symbols have been removed from QuantG.")
    found = next((srow for srow in SYMBOLS if srow["symbol"] == symbol.upper()), None)
    if not found:
        raise HTTPException(status_code=404, detail="Symbol not found")
    idx = SYMBOLS.index(found)
    lp = live_price(found["base"], idx)
    return {"symbol": found["symbol"], "name": found["name"], **lp, "series": historical_series(found["base"], 60), "source": "mock"}


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
    from server import UpstoxGateway, get_user_upstox_gateway, logger

    underlying = underlying.upper()
    if underlying not in options_helper.SUPPORTED:
        raise HTTPException(status_code=400, detail=f"Underlying must be one of {options_helper.SUPPORTED}")
    width = max(1, min(int(width or 5), 10))
    spot = {"NIFTY": 24500.0, "BANKNIFTY": 54000.0, "SENSEX": 80500.0}.get(underlying, 100.0)
    gw = await get_user_upstox_gateway(user["id"])
    spot_key = {
        "NIFTY": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "SENSEX": "BSE_INDEX|SENSEX",
    }.get(underlying)
    if gw and gw.connected and spot_key:
        try:
            quote = await asyncio.to_thread(gw.get_market_quote, [spot_key])
            spot = float(UpstoxGateway.parse_quote_ltp(quote, spot_key) or spot)
        except Exception as exc:
            logger.warning("Upstox spot quote failed for option-chain preview %s: %s", underlying, exc)
    interval = options_helper.STRIKE_INTERVALS[underlying]
    atm = options_helper.round_to_strike(float(spot), interval)
    strikes = [atm + (i * interval) for i in range(-width, width + 1)]
    exchange = options_helper.OPT_EXCHANGE[underlying]
    source = "upstox-preview" if gw and gw.connected else "preview"

    return {
        "underlying": underlying,
        "spot": round(float(spot), 2),
        "atm": atm,
        "exchange": exchange,
        "expiry": None,
        "source": source,
        "rows": [
            {
                "strike": strike,
                "ce": {"symbol": f"{underlying}{strike}CE", "ltp": round(max(1, (spot - strike) + spot * 0.01), 2)},
                "pe": {"symbol": f"{underlying}{strike}PE", "ltp": round(max(1, (strike - spot) + spot * 0.01), 2)},
            }
            for strike in strikes
        ],
    }


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
