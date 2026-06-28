from __future__ import annotations

import asyncio
import os
import secrets as _secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from core import db, get_current_user

router = APIRouter(tags=["Broker"])


class BrokerKeyReq(BaseModel):
    broker: str = "upstox"
    api_key: str
    api_secret: str
    user_id_at_broker: Optional[str] = None
    mobile_number: Optional[str] = None
    mpin: Optional[str] = None
    totp_secret_key: Optional[str] = None
    redirect_uri: Optional[str] = None
    is_sandbox: Optional[bool] = False


class BrokerKeyOut(BaseModel):
    id: str
    broker: str
    api_key_masked: str
    user_id_at_broker: Optional[str] = None
    created_at: str


class UpstoxTestOrderReq(BaseModel):
    instrument_token: str
    qty: int = Field(gt=0, description="Quantity must be > 0")
    side: str
    order_type: str = "MARKET"
    price: Optional[float] = None
    product: str = "I"
    validity: str = "DAY"
    trigger_price: Optional[float] = None
    disclosed_quantity: int = 0
    is_amo: bool = False
    market_protection: Optional[float] = -1
    confirm_live_order: bool = False


class UpstoxSubscribeReq(BaseModel):
    instruments: List[str]
    mode: str = "ltpc"


@router.post("/broker/keys", response_model=BrokerKeyOut)
async def save_broker_keys(req: BrokerKeyReq, user=Depends(get_current_user)):
    from server import _UPSTOX_GATEWAYS, _mask_secret, encrypt_secret

    broker = (req.broker or "upstox").strip().lower()
    if broker != "upstox":
        raise HTTPException(400, "QuantG is configured for Upstox-only execution")
    existing = await db.broker_keys.find_one({"user_id": user["id"], "broker": broker})
    doc = {
        "id": (existing or {}).get("id", str(uuid.uuid4())),
        "user_id": user["id"],
        "broker": broker,
        "api_key": encrypt_secret(req.api_key),
        "api_secret": encrypt_secret(req.api_secret),
        "user_id_at_broker": req.user_id_at_broker,
        "created_at": (existing or {}).get("created_at", datetime.now(timezone.utc).isoformat()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if broker == "upstox":
        redirect_uri = (req.redirect_uri or (existing or {}).get("redirect_uri") or os.environ.get("UPSTOX_REDIRECT_URI") or "").strip()
        if not redirect_uri:
            redirect_uri = "https://www.quantgtrade.com/api/broker/upstox/callback"
        doc["redirect_uri"] = redirect_uri
        doc["is_sandbox"] = bool(req.is_sandbox)
        # Stop the prior feed before evicting, otherwise its daemon thread keeps
        # reconnecting forever (orphaned feed).
        _old_gw = _UPSTOX_GATEWAYS.pop(user["id"], None)
        if _old_gw is not None:
            try:
                await asyncio.to_thread(_old_gw.stop_market_data_ws)
            except Exception:
                pass
    await db.broker_keys.update_one(
        {"user_id": user["id"], "broker": broker},
        {"$set": doc},
        upsert=True,
    )
    return BrokerKeyOut(
        id=doc["id"],
        broker=doc["broker"],
        api_key_masked=_mask_secret(req.api_key),
        user_id_at_broker=req.user_id_at_broker,
        created_at=doc["created_at"],
    )


@router.get("/broker/keys", response_model=List[BrokerKeyOut])
async def list_broker_keys(user=Depends(get_current_user)):
    from server import _mask_secret, decrypt_secret

    rows = await db.broker_keys.find({"user_id": user["id"], "broker": "upstox"}, {"_id": 0}).to_list(50)
    out = []
    for row in rows:
        key = decrypt_secret(row.get("api_key"))
        out.append(BrokerKeyOut(
            id=row["id"],
            broker=row["broker"],
            api_key_masked=_mask_secret(key),
            user_id_at_broker=row.get("user_id_at_broker"),
            created_at=row["created_at"],
        ))
    return out


@router.delete("/broker/keys/{key_id}")
async def delete_broker_key(key_id: str, user=Depends(get_current_user)):
    res = await db.broker_keys.delete_one({"id": key_id, "user_id": user["id"]})
    return {"deleted": res.deleted_count}


@router.get("/upstox/data-health")
async def upstox_data_health(user=Depends(get_current_user)):
    from server import feed_health_status, get_user_settings, get_user_upstox_gateway

    settings = await get_user_settings(user["id"])
    gateway = await get_user_upstox_gateway(user["id"])
    gateway_status = gateway.status() if gateway else {"connected": False, "feed_status": {"connected": False}}
    latest_ticks = gateway.latest_ticks() if gateway else {}
    feed_health = feed_health_status(gateway_status, latest_ticks)
    meta = await db.upstox_instrument_sync_meta.find_one({"_id": "daily-json"}, {"_id": 0}) or {}
    suspended_count = await db.upstox_suspended_instruments.count_documents({})
    active_count = await db.upstox_instruments.count_documents({"suspended": {"$ne": True}})
    return {
        "ok": feed_health.get("readiness") == "READY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "broker": "upstox",
        "mode": "paper" if settings.get("paper_mode", True) else "live",
        "paper_realism": {
            "mode": settings.get("paper_realism_mode") or "UPSTOX_LIKE",
            "upstox_like_charges": True,
            "simulated_fallback_allowed": bool(settings.get("allow_simulated_prices")),
        },
        "live_auto_trading_enabled": False,
        "feed": feed_health,
        "gateway": gateway_status,
        "instrument_sync": meta,
        "instrument_counts": {"active": active_count, "suspended": suspended_count},
        "readiness_checks": {
            "oauth_connected": bool(gateway_status.get("connected")),
            "instrument_master_synced": bool(meta.get("completed_at")),
            "feed_ready": feed_health.get("readiness") == "READY",
            "quote_fresh": not bool(feed_health.get("quote_stale")),
            "live_auto_trading_disabled_by_default": True,
        },
    }


@router.post("/upstox/instruments/sync")
async def upstox_instruments_sync(force: bool = False, user=Depends(get_current_user)):
    from server import sync_upstox_instruments

    return await sync_upstox_instruments(db, force=force)


@router.post("/upstox/quality-system/migrate")
async def upstox_quality_system_migrate(all_users: bool = False, user=Depends(get_current_user)):
    from server import migrate_all_users_to_upstox_quality_system, migrate_user_to_upstox_quality_system

    if all_users and user.get("role") not in {"admin", "owner"}:
        raise HTTPException(status_code=403, detail="Admin role required to migrate all users.")
    if all_users:
        return await migrate_all_users_to_upstox_quality_system()
    result = await migrate_user_to_upstox_quality_system(user["id"])
    return {"ok": True, "user_id": user["id"], **result}


@router.get("/upstox/option-chain")
async def upstox_option_chain(
    underlying: str = "NIFTY",
    expiry_date: Optional[str] = None,
    user=Depends(get_current_user),
):
    from server import REMOVED_COMMODITY_UNDERLYINGS, get_user_upstox_gateway

    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway or not gateway.connected:
        raise HTTPException(status_code=400, detail="Reconnect Upstox before loading option chain.")
    underlying = underlying.upper()
    spot_keys = {
        "NIFTY": "NSE_INDEX|Nifty 50",
        "BANKNIFTY": "NSE_INDEX|Nifty Bank",
        "SENSEX": "BSE_INDEX|SENSEX",
    }
    if underlying in REMOVED_COMMODITY_UNDERLYINGS:
        raise HTTPException(status_code=410, detail="MCX commodity option chains have been removed from QuantG.")
    spot_key = spot_keys.get(underlying)
    if not spot_key:
        raise HTTPException(status_code=400, detail=f"Unsupported option-chain underlying: {underlying}")
    if not expiry_date:
        from datetime import date as _date, timedelta as _td

        expiry_weekday = 4 if underlying == "SENSEX" else 3
        today = _date.today()
        days_ahead = (expiry_weekday - today.weekday()) % 7
        nearest = today + _td(days=days_ahead)
        expiry_date = nearest.strftime("%Y-%m-%d")
    try:
        chain = await asyncio.to_thread(gateway.get_option_chain, spot_key, expiry_date)
    except RuntimeError as exc:
        msg = str(exc)
        if "401" in msg or "UDAPI100050" in msg:
            raise HTTPException(status_code=400, detail="Reconnect Upstox before loading option chain.")
        raise HTTPException(status_code=502, detail=f"Upstox option chain unavailable: {msg}")
    rows = (chain or {}).get("data") or []
    return {
        "ok": True,
        "underlying": underlying,
        "instrument_key": spot_key,
        "expiry_date": expiry_date,
        "row_count": len(rows),
        "data": rows,
    }


@router.post("/upstox/webhook")
async def upstox_webhook(request: Request):
    from server import apply_broker_truth_event

    payload = await request.json()
    result = await apply_broker_truth_event(db, payload, source="webhook")
    return {"ok": True, **result}


@router.get("/upstox/reconciliation")
async def upstox_reconciliation(user=Depends(get_current_user)):
    from server import broker_reconciliation_summary, get_user_upstox_gateway

    gateway = await get_user_upstox_gateway(user["id"])
    return await broker_reconciliation_summary(db, user["id"], gateway)


@router.post("/upstox/exit-all")
async def upstox_exit_all(
    segment: Optional[str] = None,
    tag: Optional[str] = None,
    user=Depends(get_current_user),
):
    from server import get_user_settings, get_user_upstox_gateway, logger

    settings = await get_user_settings(user["id"])
    if bool(settings.get("paper_mode", True)):
        raise HTTPException(status_code=400, detail="Exit All Positions is disabled in paper mode.")
    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway or not gateway.connected:
        raise HTTPException(status_code=400, detail="Reconnect Upstox before using Exit All Positions.")
    if tag:
        logger.warning("Upstox tag-based exit requested user=%s tag=%s; Upstox may exit total instrument quantity shared by multiple strategies.", user["id"], tag)
    result = await asyncio.to_thread(gateway.exit_all_positions, segment=segment, tag=tag)
    await db.upstox_exit_all_events.insert_one({
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "segment": segment,
        "tag": tag,
        "raw": result,
        "warning": "Tag-based exit can affect total instrument quantity if multiple strategies share the same instrument.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return {
        "ok": True,
        "warning": "Tag-based exit can affect total instrument quantity if multiple strategies share the same instrument.",
        "raw": result,
    }


@router.get("/zerodha/login-url")
async def zerodha_login_url(user=Depends(get_current_user)):
    raise HTTPException(status_code=410, detail="Zerodha Kite has been removed. QuantG supports Upstox only.")


@router.post("/zerodha/exchange")
async def zerodha_exchange(user=Depends(get_current_user)):
    raise HTTPException(status_code=410, detail="Zerodha Kite has been removed. QuantG supports Upstox only.")


@router.get("/zerodha/status")
async def zerodha_status(user=Depends(get_current_user)):
    return {"connected": False, "removed": True, "reason": "zerodha_removed_upstox_only"}


@router.post("/zerodha/disconnect")
async def zerodha_disconnect(user=Depends(get_current_user)):
    await db.broker_keys.delete_many({"user_id": user["id"], "broker": {"$in": ["zerodha", "kite"]}})
    return {"disconnected": True, "removed": True}


@router.get("/broker/upstox/config")
async def upstox_config(request: Request, user=Depends(get_current_user)):
    from server import _upstox_redirect_uri

    keys = await db.broker_keys.find_one({"user_id": user["id"], "broker": "upstox"}, {"_id": 0})
    redirect_uri = _upstox_redirect_uri(request, keys)
    return {
        "redirect_uri": redirect_uri,
        "register_in_upstox_portal": True,
        "hint": "Add this exact Redirect URL in the Upstox Developer app, then save API Key + Secret here.",
    }


@router.get("/broker/upstox/login")
async def upstox_login(request: Request, user=Depends(get_current_user)):
    from server import _mask_secret, _upstox_redirect_uri, decrypt_secret, get_user_upstox_gateway

    keys = await db.broker_keys.find_one({"user_id": user["id"], "broker": "upstox"}, {"_id": 0})
    if not keys:
        raise HTTPException(status_code=400, detail="Save Upstox API key and secret first.")
    api_key = os.environ.get("UPSTOX_API_KEY") or decrypt_secret(keys.get("api_key"))
    api_secret = os.environ.get("UPSTOX_API_SECRET") or decrypt_secret(keys.get("api_secret"))
    if not api_key or not api_secret:
        raise HTTPException(
            status_code=400,
            detail="Upstox API key or secret is missing. Re-save your Upstox credentials on this page.",
        )
    redirect_uri = _upstox_redirect_uri(request, keys)
    await db.broker_keys.update_one(
        {"user_id": user["id"], "broker": "upstox"},
        {"$set": {"redirect_uri": redirect_uri, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    gateway = await get_user_upstox_gateway(user["id"], fresh=True)
    if not gateway:
        raise HTTPException(status_code=400, detail="Upstox gateway could not be initialized.")
    gateway.redirect_uri = redirect_uri
    state = _secrets.token_urlsafe(24)
    await db.broker_oauth_states.insert_one({
        "state": state,
        "broker": "upstox",
        "user_id": user["id"],
        "redirect_uri": redirect_uri,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    try:
        url = gateway.build_login_url(state=state, redirect_uri=redirect_uri)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Upstox login URL could not be built: {exc}. Register redirect URI exactly as: {redirect_uri}",
        )
    return {"url": url, "redirect_uri": redirect_uri, "api_key": _mask_secret(api_key, 6, 0)}


@router.get("/broker/upstox/callback", name="upstox_callback")
async def upstox_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    from server import (
        _UPSTOX_GATEWAYS,
        _UPSTOX_TOKEN_VALIDATION_CACHE,
        _public_base_url,
        _start_user_upstox_ticker,
        encrypt_secret,
        get_user_settings,
        get_user_upstox_gateway,
        logger,
    )

    if error:
        raise HTTPException(status_code=400, detail=f"Upstox OAuth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing Upstox OAuth code/state.")
    state_doc = await db.broker_oauth_states.find_one({"state": state, "broker": "upstox"}, {"_id": 0})
    if not state_doc:
        raise HTTPException(status_code=400, detail="Invalid or expired Upstox OAuth state. Start login again.")
    user_id = state_doc["user_id"]
    prior_feed_gateway = _UPSTOX_GATEWAYS.get(user_id)
    gateway = await get_user_upstox_gateway(user_id, fresh=True)
    if not gateway:
        raise HTTPException(status_code=400, detail="Upstox credentials are not configured for this user.")
    try:
        token_response = await asyncio.to_thread(gateway.exchange_code, code=code, redirect_uri=state_doc.get("redirect_uri"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Upstox token exchange failed: {exc}")
    access_token = token_response.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Upstox token response did not include access_token.")
    settings = await get_user_settings(user_id)
    if not settings.get("paper_mode", True) and str(access_token).startswith("mock_live_upstox_token"):
        raise HTTPException(status_code=400, detail="Cannot save simulated mock tokens in live production mode.")
    refresh_token = token_response.get("refresh_token")
    set_fields = {
        "access_token": encrypt_secret(str(access_token)),
        "upstox_user_id": token_response.get("user_id"),
        "access_token_obtained_at": datetime.now(timezone.utc).isoformat(),
        "token_response_meta": {
            key: value for key, value in token_response.items()
            if key not in {"access_token", "refresh_token", "extended_token"}
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if refresh_token:
        set_fields["refresh_token"] = encrypt_secret(str(refresh_token))
    await db.broker_keys.update_one(
        {"user_id": user_id, "broker": "upstox"},
        {
            "$set": set_fields,
            "$setOnInsert": {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "broker": "upstox",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        },
        upsert=True,
    )
    await db.broker_oauth_states.delete_one({"state": state, "broker": "upstox"})
    _UPSTOX_GATEWAYS.pop(user_id, None)
    _UPSTOX_TOKEN_VALIDATION_CACHE.pop(user_id, None)
    if prior_feed_gateway is not None:
        try:
            await asyncio.to_thread(prior_feed_gateway.stop_market_data_ws)
        except Exception as stop_err:
            logger.warning("Upstox OAuth: failed to stop prior feed user=%s: %s", user_id, stop_err)
    try:
        ticker_result = await _start_user_upstox_ticker(user_id)
        open_positions = await db.strategy_positions.find(
            {"user_id": user_id, "status": {"$in": ["OPEN", "FILLED", "EXITING"]}},
            {"instrument_key": 1, "_id": 0},
        ).to_list(200)
        position_tokens = [
            position["instrument_key"] for position in open_positions
            if position.get("instrument_key") and "|" in str(position.get("instrument_key", ""))
        ]
        if position_tokens:
            gateway = await get_user_upstox_gateway(user_id)
            if gateway:
                await asyncio.to_thread(gateway.start_market_data_ws, position_tokens, "full")
        logger.info(
            "Upstox OAuth connected, token stored, feed restarted for user=%s started=%s pos_tokens=%d",
            user_id,
            ticker_result.get("started"),
            len(position_tokens),
        )
    except Exception as feed_err:
        logger.warning("Upstox OAuth: feed restart after token store failed user=%s: %s", user_id, feed_err)
    base = _public_base_url(None)
    return RedirectResponse(url=f"{base}/broker-keys?upstox=connected", status_code=303)


@router.post("/broker/upstox/order/test")
async def upstox_test_order(req: UpstoxTestOrderReq, user=Depends(get_current_user)):
    from server import UpstoxGateway, _new_execution_tag, _place_upstox_order

    side = req.side.upper()
    order_type = req.order_type.upper()
    if side not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="side must be BUY or SELL")
    if order_type not in {"MARKET", "LIMIT", "SL", "SL-M"}:
        raise HTTPException(status_code=400, detail="order_type must be MARKET, LIMIT, SL or SL-M")
    tag = _new_execution_tag()
    preview = {
        "instrument_token": req.instrument_token,
        "quantity": req.qty,
        "side": side,
        "order_type": order_type,
        "product": UpstoxGateway.normalize_product(req.product),
        "price": req.price,
        "validity": req.validity,
        "trigger_price": req.trigger_price,
        "disclosed_quantity": req.disclosed_quantity,
        "is_amo": req.is_amo,
        "market_protection": req.market_protection,
        "tag": tag,
    }
    if not req.confirm_live_order:
        return {
            "ok": False,
            "dry_run": True,
            "message": "Set confirm_live_order=true to place this real Upstox test order.",
            "preview": preview,
        }
    result = await _place_upstox_order(
        user["id"],
        instrument_token=req.instrument_token,
        side=side,
        quantity=req.qty,
        order_type=order_type,
        product=req.product,
        price=req.price,
        tag=tag,
        validity=req.validity,
        trigger_price=req.trigger_price,
        disclosed_quantity=req.disclosed_quantity,
        is_amo=req.is_amo,
        market_protection=req.market_protection,
    )
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "symbol": req.instrument_token,
        "side": side,
        "qty": req.qty,
        "filled_qty": None,
        "pending_qty": None,
        "status_message": None,
        "realized_pnl": 0.0,
        "order_type": order_type,
        "price": float(req.price or 0),
        "brokerage": 0.0,
        "product": UpstoxGateway.normalize_product(req.product),
        "status": "OPEN",
        "mode": "live",
        "broker": "upstox",
        "broker_order_id": result.get("broker_order_id"),
        "execution_tag": tag,
        "execution_attempts": result.get("attempts"),
        "source": "manual-upstox-test",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "instrument_token": req.instrument_token,
    }
    await db.orders.insert_one(doc)
    doc.pop("_id", None)
    doc.pop("user_id", None)
    return {"ok": True, "order": doc, "broker_response": result.get("raw")}


@router.get("/broker/upstox/positions")
async def upstox_positions(user=Depends(get_current_user)):
    from server import get_user_upstox_gateway

    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway or not gateway.connected:
        raise HTTPException(status_code=400, detail="Upstox is not connected.")
    return await asyncio.to_thread(gateway.get_positions)


@router.get("/broker/upstox/orders")
async def upstox_orders(user=Depends(get_current_user)):
    from server import get_user_upstox_gateway

    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway or not gateway.connected:
        raise HTTPException(status_code=400, detail="Upstox is not connected.")
    return await asyncio.to_thread(gateway.get_order_book)


@router.get("/broker/upstox/quote")
async def upstox_quote(instrument_key: str, user=Depends(get_current_user)):
    from server import get_user_upstox_gateway

    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway or not gateway.connected:
        raise HTTPException(status_code=400, detail="Upstox is not connected.")
    keys = [part.strip() for part in instrument_key.split(",") if part.strip()]
    return await asyncio.to_thread(gateway.get_market_quote, keys)


@router.post("/broker/upstox/market-data/start")
async def upstox_market_data_start(req: UpstoxSubscribeReq, user=Depends(get_current_user)):
    from server import get_user_upstox_gateway, logger

    gateway = await get_user_upstox_gateway(user["id"])
    if not gateway or not gateway.connected:
        raise HTTPException(status_code=400, detail="Upstox is not connected.")
    valid_instruments: List[str] = []
    rejected: List[Dict[str, str]] = []
    for instrument_key in req.instruments:
        key = str(instrument_key or "").strip()
        if key.startswith("MCX_FO|"):
            rejected.append({"instrument_key": key, "reason": "mcx_removed"})
            logger.warning("Rejecting removed MCX websocket subscription key=%s", key)
            continue
        valid_instruments.append(key)
    if not valid_instruments:
        raise HTTPException(status_code=400, detail={"message": "No valid Upstox instrument_key supplied.", "rejected": rejected})
    result = await asyncio.to_thread(gateway.start_market_data_ws, valid_instruments, req.mode)
    result["rejected"] = rejected
    return result


@router.get("/upstox/status")
async def upstox_status(user=Depends(get_current_user)):
    from server import get_user_upstox_status

    return await get_user_upstox_status(user["id"])


@router.get("/broker/upstox/status")
async def broker_upstox_status(user=Depends(get_current_user)):
    from server import get_user_upstox_status

    return await get_user_upstox_status(user["id"])


@router.get("/brokers/status")
async def brokers_status(user=Depends(get_current_user)):
    from server import APP_VERSION, get_user_settings, get_user_upstox_status

    settings = await get_user_settings(user["id"])
    upstox = await get_user_upstox_status(user["id"])
    return {
        "version": APP_VERSION,
        "preferences": {
            "data_broker": settings.get("data_broker"),
            "execution_broker": settings.get("execution_broker"),
            "fallback_broker": settings.get("fallback_broker"),
        },
        "brokers": {"upstox": upstox},
    }


@router.get("/diagnostics/health")
async def diagnostics_health(user=Depends(get_current_user)):
    from server import get_user_upstox_status

    user_id = user["id"]
    strategies = await db.strategies.find({"user_id": user_id}, {"_id": 0}).to_list(100)
    strategy_diagnostics = [
        {
            "id": strategy.get("id"),
            "name": strategy.get("name"),
            "status": strategy.get("status"),
            "mode": strategy.get("mode"),
            "symbol": strategy.get("symbol"),
            "broker": strategy.get("broker", "upstox"),
        }
        for strategy in strategies
    ]
    upstox = await get_user_upstox_status(user_id)
    gateway = upstox.get("gateway") or {}
    feed_diagnostics = {
        "connected": bool(upstox.get("connected") or gateway.get("connected")),
        "ticks_received": gateway.get("ticks", 0),
        "last_quote_time": gateway.get("last_tick_at"),
        "last_error": gateway.get("last_error"),
    }
    locks = await db.strategy_position_locks.find({"user_id": user_id}).to_list(100)
    lock_diagnostics = [
        {
            "strategy_id": lock.get("strategy_id"),
            "trading_symbol": lock.get("trading_symbol"),
            "instrument_key": lock.get("instrument_key"),
            "created_at": lock.get("created_at"),
            "expires_at": lock.get("expires_at"),
        }
        for lock in locks
    ]
    rejected_orders = await db.orders.find(
        {"user_id": user_id, "status": {"$in": ["REJECTED", "CANCELLED", "FAILED"]}},
        {"_id": 0},
    ).sort("created_at", -1).limit(5).to_list(5)
    order_diagnostics = [
        {
            "id": order.get("id"),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "qty": order.get("qty"),
            "status": order.get("status"),
            "status_message": order.get("status_message"),
            "created_at": order.get("created_at"),
        }
        for order in rejected_orders
    ]
    return {
        "status": "healthy",
        "strategies": strategy_diagnostics,
        "feed": feed_diagnostics,
        "position_locks": lock_diagnostics,
        "recent_failed_orders": order_diagnostics,
    }


@router.get("/broker/health")
async def broker_health(user=Depends(get_current_user)):
    from server import APP_VERSION, _HISTORY_CACHE, get_user_settings, get_user_upstox_status

    settings = await get_user_settings(user["id"])
    upstox = await get_user_upstox_status(user["id"])
    return {
        "version": APP_VERSION,
        "preferences": {
            "data_broker": settings.get("data_broker"),
            "execution_broker": settings.get("execution_broker"),
            "fallback_broker": settings.get("fallback_broker"),
        },
        "upstox": upstox,
        "brokers": {"upstox": upstox},
        "cache": {"history_entries": len(_HISTORY_CACHE)},
    }


@router.post("/gateway/check-all")
async def gateway_check_all(user=Depends(get_current_user)):
    from server import check_all_gateways_at_market_open

    results = await check_all_gateways_at_market_open()
    return {"results": results, "checked_at": datetime.now(timezone.utc).isoformat()}


@router.get("/gateway/status")
async def gateway_status_all(user=Depends(get_current_user)):
    from server import _GATEWAY_BLOCKED, _UPSTOX_GATEWAYS

    users = await db.users.find({}, {"_id": 0, "id": 1, "email": 1}).to_list(1000)
    out = []
    for row in users:
        user_id = row["id"]
        gateway = _UPSTOX_GATEWAYS.get(user_id)
        gateway_status = gateway.status() if gateway else {}
        health_doc = await db.gateway_health.find_one({"user_id": user_id}, {"_id": 0})
        out.append({
            "user_id": user_id,
            "email": row.get("email"),
            "ws_running": gateway_status.get("ws_running", False),
            "gateway_connected": gateway_status.get("connected", False),
            "gateway_blocked": _GATEWAY_BLOCKED.get(user_id, False),
            "token_refresh_count": gateway_status.get("token_refresh_count", 0),
            "token_refresh_last_at": gateway_status.get("token_refresh_last_at"),
            "token_push_sent_at": (health_doc or {}).get("token_push_sent_at"),
            "token_push_ok": (health_doc or {}).get("token_push_ok"),
            "last_checked_at": (health_doc or {}).get("checked_at"),
        })
    return {"gateways": out, "total": len(out)}


@router.post("/webhook/upstox/token/{user_id}")
async def upstox_token_webhook(user_id: str, request: Request):
    from server import (
        _GATEWAY_BLOCKED,
        _UPSTOX_GATEWAYS,
        _UPSTOX_TOKEN_VALIDATION_CACHE,
        _start_user_upstox_ticker,
        encrypt_secret,
        get_user_upstox_gateway,
        logger,
    )

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    new_token = (
        payload.get("access_token")
        or payload.get("token")
        or payload.get("data", {}).get("access_token")
    )
    if not new_token:
        logger.warning("FIX7 WEBHOOK: no access_token in payload for user %s: %s", user_id, payload)
        return {"ok": False, "reason": "no_access_token_in_payload"}
    try:
        encrypted = encrypt_secret(str(new_token))
    except Exception:
        encrypted = str(new_token)
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.broker_keys.update_one(
        {"user_id": user_id, "broker": "upstox"},
        {"$set": {
            "access_token": encrypted,
            "token_refreshed_at": now_iso,
            "token_refresh_source": "upstox_push_webhook",
        }},
    )
    _UPSTOX_TOKEN_VALIDATION_CACHE.pop(user_id, None)
    old_gateway = _UPSTOX_GATEWAYS.pop(user_id, None)
    if old_gateway is not None:
        try:
            await asyncio.to_thread(old_gateway.stop_market_data_ws)
        except Exception as stop_err:
            logger.warning("FIX7 WEBHOOK: failed to stop old feed user=%s: %s", user_id, stop_err)
    gateway = await get_user_upstox_gateway(user_id)
    if gateway:
        gateway.access_token = str(new_token)
        await _start_user_upstox_ticker(user_id)
        _GATEWAY_BLOCKED[user_id] = False
    logger.info("FIX7 WEBHOOK: TOKEN REFRESHED for user %s at %s - gateway reconnected", user_id, now_iso)
    return {"ok": True, "user_id": user_id, "refreshed_at": now_iso}
