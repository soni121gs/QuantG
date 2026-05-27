from __future__ import annotations

import os
import asyncio
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core import db, get_current_user

router = APIRouter(prefix="/ops", tags=["Operations"])

class OpsActionReq(BaseModel):
    note: Optional[str] = None


@router.get("/diagnostics")
async def ops_diagnostics_route(user=Depends(get_current_user)):
    from server import ops_diagnostics
    return await ops_diagnostics(user=user)


@router.post("/ticker/restart")
async def ops_restart_ticker(req: OpsActionReq = None, user=Depends(get_current_user), request: Request = None):
    # Import settings to check broker pref
    from server import get_user_settings, _start_user_kotak_ticker, _start_user_ticker, _start_user_upstox_ticker, app
    
    settings = await get_user_settings(user["id"])
    if settings.get("data_broker") == "upstox":
        return await _start_user_upstox_ticker(user["id"])
    if settings.get("data_broker") == "kotak_neo":
        return await _start_user_kotak_ticker(user["id"])
        
    tick_manager = getattr(app.state, "tick_manager", None)
    if tick_manager:
        try:
            ticker = tick_manager._tickers.get(user["id"])
            if ticker:
                ticker.stop()
        except Exception as e:
            import logging
            logging.getLogger("quantdesk.ops").warning(f"ticker stop from ops failed: {e}")
            
    return await _start_user_ticker(user["id"])


@router.post("/orders/sync")
async def ops_sync_orders(req: OpsActionReq = None, user=Depends(get_current_user)):
    from server import (
        get_user_kite, 
        _ORDER_SYNC_CACHE, 
        _sync_kite_order_statuses, 
        _stale_local_open_orders, 
        _sync_kotak_order_statuses, 
        _sync_upstox_order_statuses,
        _sync_strategy_positions_with_broker
    )
    
    kite, status = await get_user_kite(user["id"])
    _ORDER_SYNC_CACHE.pop(user["id"], None)
    sync = await _sync_kite_order_statuses(user["id"], kite) if kite else {"checked": 0, "updated": 0, "reason": status.get("reason", "zerodha_not_connected")}
    stale = await _stale_local_open_orders(user["id"], kite) if kite else {"fixed": 0, "reason": status.get("reason", "zerodha_not_connected")}
    kotak_sync = await _sync_kotak_order_statuses(user["id"])
    upstox_sync = await _sync_upstox_order_statuses(user["id"], force=True)
    position_sync = await _sync_strategy_positions_with_broker(user["id"], kite)
    return {"ok": True, "sync": sync, "stale": stale, "kotak_sync": kotak_sync, "upstox_sync": upstox_sync, "position_sync": position_sync}


@router.post("/auto-recover")
async def ops_auto_recover(req: OpsActionReq = None, user=Depends(get_current_user)):
    from server import (
        get_user_kite,
        _ORDER_SYNC_CACHE,
        _sync_kite_order_statuses,
        _stale_local_open_orders,
        _sync_strategy_positions_with_broker,
        _sync_upstox_order_statuses,
        _start_user_upstox_ticker,
        _start_user_ticker,
        _KOTAK_GATEWAYS,
        _sync_kotak_order_statuses,
        _start_user_kotak_ticker,
        ops_diagnostics
    )
    
    actions: List[Dict[str, Any]] = []
    kite, kite_status = await get_user_kite(user["id"])
    if kite:
        _ORDER_SYNC_CACHE.pop(user["id"], None)
        actions.append({"name": "zerodha_order_sync", "result": await _sync_kite_order_statuses(user["id"], kite)})
        actions.append({"name": "stale_order_repair", "result": await _stale_local_open_orders(user["id"], kite)})
        actions.append({"name": "strategy_position_sync", "result": await _sync_strategy_positions_with_broker(user["id"], kite)})
        ticker_result = await _start_user_ticker(user["id"])
        actions.append({"name": "zerodha_ticker_restart", "result": ticker_result})
    else:
        actions.append({"name": "zerodha_session", "skipped": True, "reason": kite_status.get("reason", "not_connected")})

    actions.append({"name": "upstox_order_sync", "result": await _sync_upstox_order_statuses(user["id"], force=True)})
    actions.append({"name": "upstox_position_sync", "result": await _sync_strategy_positions_with_broker(user["id"], kite)})
    actions.append({"name": "upstox_market_ticker", "result": await _start_user_upstox_ticker(user["id"])})

    kotak_gateway = _KOTAK_GATEWAYS.get(user["id"])
    if kotak_gateway and kotak_gateway.status().get("authenticated"):
        actions.append({"name": "kotak_order_sync", "result": await _sync_kotak_order_statuses(user["id"])})
        actions.append({"name": "kotak_position_sync", "result": await _sync_strategy_positions_with_broker(user["id"], kite)})
        order_feed = await asyncio.to_thread(kotak_gateway.subscribe_order_feed)
        actions.append({"name": "kotak_order_feed", "result": order_feed})
        market_feed = await _start_user_kotak_ticker(user["id"])
        actions.append({"name": "kotak_market_ticker", "result": market_feed})
    else:
        actions.append({"name": "kotak_order_feed", "skipped": True, "reason": "not_connected"})

    diagnostics = await ops_diagnostics(user=user)
    return {"ok": True, "actions": actions, "recovery_plan": diagnostics.get("recovery_plan")}


@router.post("/emergency-stop")
async def ops_emergency_stop(req: OpsActionReq = None, user=Depends(get_current_user)):
    from server import option_ledger
    
    now = datetime.now(timezone.utc).isoformat()
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "id": 1}).to_list(500)
    for row in strategies:
        option_ledger.set_kill_switch(True, strategy_id=row["id"])
    await db.users.update_one({"id": user["id"]}, {"$set": {"paper_mode": True, "ops_last_emergency_stop_at": now}})
    res = await db.strategies.update_many(
        {"user_id": user["id"], "status": "live"},
        {"$set": {"status": "paused", "last_error": f"Emergency stop at {now}: switched to PAPER and paused automation."}},
    )
    return {"ok": True, "paper_mode": True, "paused_strategies": res.modified_count, "disabled_strategies": len(strategies), "at": now}


@router.post("/strategies/pause-all")
async def ops_pause_all(req: OpsActionReq = None, user=Depends(get_current_user)):
    res = await db.strategies.update_many(
        {"user_id": user["id"], "status": "live"},
        {"$set": {"status": "paused", "last_error": None}},
    )
    return {"ok": True, "paused_strategies": res.modified_count}


@router.post("/strategies/enable-all")
async def ops_enable_all_strategies(req: OpsActionReq = None, user=Depends(get_current_user)):
    from server import option_ledger, _sync_option_ledger_strategy
    
    rows = await db.strategies.find({"user_id": user["id"]}, {"_id": 0}).to_list(500)
    for row in rows:
        _sync_option_ledger_strategy(row)
        option_ledger.set_kill_switch(False, strategy_id=row["id"])
    res = await db.strategies.update_many(
        {"user_id": user["id"]},
        {"$set": {"status": "live"}, "$unset": {"last_error": "", "last_signal_validation": ""}},
    )
    return {"ok": True, "enabled_strategies": res.modified_count, "ledger_enabled": len(rows)}


@router.post("/strategies/clear-errors")
async def ops_clear_strategy_errors(req: OpsActionReq = None, user=Depends(get_current_user)):
    res = await db.strategies.update_many(
        {"user_id": user["id"]},
        {"$unset": {"last_error": "", "last_signal_validation": ""}},
    )
    return {"ok": True, "updated_strategies": res.modified_count}


@router.get("/pending-users")
async def get_pending_users(user=Depends(get_current_user)):
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Access denied: Owner role required.")
    pending = await db.users.find({"approved": False, "role": {"$ne": "owner"}}, {"_id": 0, "password_hash": 0}).to_list(100)
    return pending


@router.post("/users/{user_id}/approve")
async def approve_user(user_id: str, user=Depends(get_current_user)):
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Access denied: Owner role required.")
    res = await db.users.update_one({"id": user_id}, {"$set": {"approved": True, "status": "approved"}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
        
    # Seed default strategies for the newly approved trader
    from server import seed_default_strategies_for_user
    await seed_default_strategies_for_user(user_id)
    
    return {"ok": True, "message": "User account approved and default strategies initialized."}


@router.post("/users/{user_id}/reject")
async def reject_user(user_id: str, user=Depends(get_current_user)):
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Access denied: Owner role required.")
    res = await db.users.delete_one({"id": user_id, "role": {"$ne": "owner"}})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="User not found or cannot be rejected")
    return {"ok": True, "message": "User registration rejected and deleted."}
