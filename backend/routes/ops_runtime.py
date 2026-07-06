from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from core import db, get_current_user

router = APIRouter(tags=["Ops Runtime"])


@router.post("/ops/v12/upstox-retailer/activate")
async def activate_v12_upstox_retailer(user=Depends(get_current_user)):
    from server import (
        APP_VERSION,
        _sync_option_ledger_strategy,
        migrate_user_to_v12_upstox,
        seed_default_strategies_for_user,
    )

    inserted = await seed_default_strategies_for_user(user["id"])
    migrated = await migrate_user_to_v12_upstox(user["id"])
    await db.strategies.update_many(
        {"user_id": user["id"], "status": {"$nin": ["live", "paused"]}},
        {"$set": {"status": "live", "broker": "upstox", "mode": "live"}},
    )
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0}).to_list(500)
    for row in strategies:
        _sync_option_ledger_strategy(row)
    return {
        "ok": True,
        "version": APP_VERSION,
        "inserted": inserted,
        "migrated": migrated,
        "live_strategies": sum(1 for s in strategies if s.get("status") == "live"),
        "message": "QuantG v12 Upstox retailer profile is active for NSE/NFO/BSE/BFO.",
    }


@router.post("/ops/squareoff-all")
async def squareoff_all_positions(user=Depends(get_current_user)):
    from server import _place_order_core, get_user_settings, get_user_upstox_gateway, list_positions

    settings = await get_user_settings(user["id"])
    positions = await list_positions(user)
    if not positions:
        return {"ok": True, "closed": [], "failed": []}

    closed, failed = [], []
    now = datetime.now(timezone.utc).isoformat()
    paper = settings.get("paper_mode", True)
    if not paper:
        gateway = await get_user_upstox_gateway(user["id"])
        if not gateway or not gateway.connected:
            raise HTTPException(status_code=400, detail="auth failed: Upstox is not connected.")

    for p in positions:
        symbol = p.get("symbol")
        qty = int(p.get("qty") or 0)
        if not symbol or qty == 0:
            continue
        side = "SELL" if qty > 0 else "BUY"
        abs_qty = abs(qty)
        try:
            if paper:
                _sq_px = None
                try:
                    _p = float(p.get("ltp") or 0)
                    if _p > 0:
                        _sq_px = _p
                except (TypeError, ValueError):
                    _sq_px = None
                res = await _place_order_core(
                    user_id=user["id"],
                    symbol=symbol,
                    side=side,
                    qty=abs_qty,
                    order_type="MARKET",
                    price=_sq_px,
                    product=p.get("product") or settings.get("default_product", "MIS"),
                    source="squareoff-all",
                    exchange=p.get("exchange") or "NSE",
                    idempotency_key=f"squareoff-all:{symbol}:{now}",
                    is_exit_order=True,
                    exit_reason="squareoff-all",
                )
                closed.append({"symbol": symbol, "qty": abs_qty, "side": side, "mode": "paper", "order_id": res.get("id")})
            else:
                exchange = p.get("exchange") or ("NFO" if str(symbol).upper().endswith(("CE", "PE")) else "NSE")
                option_contract = None
                if exchange in {"NFO", "BFO", "MCX"} or str(symbol).upper().endswith(("CE", "PE")):
                    token = str(p.get("instrument_token") or "").strip()
                    if "|" not in token:
                        failed.append({"symbol": symbol, "error": "Upstox instrument_key missing. Exit this position in Upstox, then sync broker state."})
                        continue
                    option_contract = {
                        "tradingsymbol": str(symbol).upper(),
                        "exchange": exchange,
                        "instrument_token": token,
                        "lot_size": max(1, abs_qty),
                        "transaction_type": side,
                    }
                res = await _place_order_core(
                    user_id=user["id"],
                    symbol=str(symbol).upper(),
                    side=side,
                    qty=1 if option_contract else abs_qty,
                    order_type="MARKET",
                    product=p.get("product") or settings.get("default_product", "MIS"),
                    source="squareoff",
                    exchange=exchange,
                    option_contract=option_contract,
                    idempotency_key=f"squareoff-live:{symbol}:{now}",
                )
                closed.append({"symbol": symbol, "qty": abs_qty, "side": side, "mode": "live", "order_id": res.get("broker_order_id")})
        except Exception as e:
            failed.append({"symbol": symbol, "error": str(e)})
    return {"ok": not failed, "closed": closed, "failed": failed}


@router.get("/ops/trading-ready")
async def trading_ready_check(user=Depends(get_current_user)):
    from server import (
        _is_order_market_open,
        _latest_candle_fresh_for_live,
        get_user_settings,
        get_user_upstox_gateway,
        get_user_upstox_status,
    )

    user_id = user["id"]
    checks: Dict[str, Any] = {}

    upstox_status = await get_user_upstox_status(user_id)
    checks["broker_auth"] = {
        "ok": bool(upstox_status.get("token_valid")),
        "detail": upstox_status.get("token_state", "unknown"),
        "critical": True,
    }

    gw = await get_user_upstox_gateway(user_id)
    gw_connected = bool(gw and gw.connected)
    checks["gateway_connected"] = {
        "ok": gw_connected,
        "detail": "connected" if gw_connected else "gateway not initialised",
        "critical": True,
    }

    ws_active = bool(gw and getattr(gw, "_ws_thread", None) and getattr(gw._ws_thread, "is_alive", lambda: False)())
    checks["websocket_feed"] = {
        "ok": ws_active,
        "detail": "active" if ws_active else "not started (will auto-start on first strategy scan)",
        "critical": False,
    }

    nifty_ok = False
    nifty_detail = "not tested"
    if gw_connected:
        try:
            candles = await asyncio.to_thread(
                gw.get_historical_candles, "NSE_INDEX|Nifty 50", "5minute", 3
            )
            nifty_ok = bool(candles and len(candles) >= 2)
            nifty_detail = f"{len(candles or [])} bars" if nifty_ok else "returned empty"
        except Exception as exc:
            nifty_detail = str(exc)[:120]
    checks["nifty_candles"] = {"ok": nifty_ok, "detail": nifty_detail, "critical": True}

    bnk_ok = False
    bnk_detail = "not tested"
    if gw_connected:
        try:
            candles = await asyncio.to_thread(
                gw.get_historical_candles, "NSE_INDEX|Nifty Bank", "5minute", 3
            )
            bnk_ok = bool(candles and len(candles) >= 2)
            bnk_detail = f"{len(candles or [])} bars" if bnk_ok else "returned empty"
        except Exception as exc:
            bnk_detail = str(exc)[:120]
    checks["banknifty_candles"] = {"ok": bnk_ok, "detail": bnk_detail, "critical": True}

    sensex_ok = False
    sensex_detail = "not tested"
    if gw_connected:
        try:
            candles = await asyncio.to_thread(
                gw.get_historical_candles, "BSE_INDEX|SENSEX", "5minute", 3
            )
            sensex_ok = bool(candles and len(candles) >= 2)
            sensex_detail = f"{len(candles or [])} bars" if sensex_ok else "returned empty"
        except Exception as exc:
            sensex_detail = str(exc)[:120]
    checks["sensex_candles"] = {"ok": sensex_ok, "detail": sensex_detail, "critical": False}

    market_open = _is_order_market_open("NSE")
    if nifty_ok and market_open and gw_connected:
        try:
            candles = await asyncio.to_thread(
                gw.get_historical_candles, "NSE_INDEX|Nifty 50", "5minute", 1
            )
            freshness = _latest_candle_fresh_for_live(candles or [], "NSE")
            checks["candle_freshness"] = {
                "ok": bool(freshness.get("fresh")),
                "detail": freshness.get("reason", "unknown"),
                "age_sec": freshness.get("age_sec"),
                "critical": True,
            }
        except Exception as exc:
            checks["candle_freshness"] = {"ok": False, "detail": str(exc)[:120], "critical": True}
    else:
        checks["candle_freshness"] = {
            "ok": True,
            "detail": "market closed \u2014 freshness check skipped" if not market_open else "candle fetch failed above",
            "critical": False,
        }

    settings = await get_user_settings(user_id)
    paper_mode = bool(settings.get("paper_mode", True))
    checks["paper_mode"] = {
        "ok": True,
        "detail": "paper" if paper_mode else "LIVE",
        "critical": False,
    }

    critical_checks = [v for v in checks.values() if v.get("critical")]
    all_critical_ok = all(c["ok"] for c in critical_checks)
    ready = all_critical_ok

    # Fetch strategy counts for strategies_armed check (additive, read-only)
    live_count = await db.strategies.count_documents({"user_id": user_id, "status": "live"})
    not_auto_arming = await db.strategies.count_documents({"user_id": user_id, "status": {"$in": ["paused", "draft"]}})

    checks["strategies_armed"] = {
        "ok": live_count > 0,
        "detail": f"{live_count} live; {not_auto_arming} paused/draft won't auto-arm at 9AM",
        "live_count": live_count,
        "paused_or_draft_count": not_auto_arming,
        "critical": False,
    }

    warnings = []
    if live_count == 0:
        warnings.append("No strategies armed — arm at least one strategy to trade today.")

    return {
        "ready": ready,
        "trading_mode": "paper" if paper_mode else "live",
        "market_open": market_open,
        "checks": checks,
        "warnings": warnings,
        "summary": (
            "All systems go — ready to trade."
            if ready
            else "One or more critical checks failed. See checks for details."
        ),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
