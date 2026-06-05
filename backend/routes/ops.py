from __future__ import annotations

import os
import asyncio
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core import db, get_current_user

router = APIRouter(prefix="/ops", tags=["Operations"])

class OpsActionReq(BaseModel):
    note: Optional[str] = None
    confirm: Optional[bool] = False


RECONCILIATION_RESOLVE_PHRASE = "RESOLVE_RECONCILIATION_AFTER_MANUAL_BROKER_CHECK"


def _public_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    if not doc:
        return {}
    out = dict(doc)
    out.pop("_id", None)
    return out


async def _append_ops_audit_event(user_id: str, event_type: str, payload: Optional[Dict[str, Any]] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    body = payload or {}
    event_id = f"ops_{uuid.uuid4().hex}"
    event = {
        "id": event_id,
        "user_id": user_id,
        "event_type": event_type,
        "payload": body,
        "created_at": now,
        "source": "ops",
    }
    await db["risk_events"].insert_one(event)
    await db["outbox_events"].insert_one({
        "id": f"outbox_{uuid.uuid4().hex}",
        "aggregate_type": "ops",
        "aggregate_id": user_id,
        "event_type": event_type,
        "payload": body,
        "user_id": user_id,
        "status": "pending",
        "created_at": now,
    })


async def _reconciliation_break_snapshot(user_id: str) -> Dict[str, Any]:
    unknown_orders = await db["orders"].find(
        {"user_id": user_id, "mode": "live", "status": "UNKNOWN_NEEDS_REVIEW"},
        {"_id": 0},
    ).to_list(500)
    review_reservations = await db["risk_reservations"].find(
        {"user_id": user_id, "status": "NEEDS_REVIEW"},
        {"_id": 0},
    ).to_list(500)
    active_reservations = await db["risk_reservations"].find(
        {"user_id": user_id, "status": "ACTIVE"},
        {"_id": 0},
    ).to_list(500)
    active_positions = await db["strategy_positions"].find(
        {
            "user_id": user_id,
            "mode": "live",
            "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
        },
        {"_id": 0},
    ).to_list(500)
    recon_state = await db["risk_state"].find_one({
        "$or": [
            {"_id": f"position_reconciliation:{user_id}"},
            {"_id": "position_reconciliation", "$or": [{"user_id": user_id}, {"user_id": {"$exists": False}}]},
        ]
    }) or {}
    mismatch_detected = bool(recon_state.get("mismatch_detected"))
    blockers = {
        "position_reconciliation_mismatch": mismatch_detected,
        "unknown_live_orders": len(unknown_orders),
        "reservations_needing_review": len(review_reservations),
        "active_live_reservations": len(active_reservations),
    }
    can_resolve_position_reconciliation = (
        mismatch_detected
        and blockers["unknown_live_orders"] == 0
        and blockers["reservations_needing_review"] == 0
    )
    return {
        "ok": True,
        "user_id": user_id,
        "reconciliation_state": _public_doc(recon_state),
        "blockers": blockers,
        "unknown_live_orders": unknown_orders,
        "reservations_needing_review": review_reservations,
        "active_live_reservations": active_reservations,
        "active_live_strategy_positions": active_positions,
        "can_resolve_position_reconciliation": can_resolve_position_reconciliation,
        "resolve_required_note": RECONCILIATION_RESOLVE_PHRASE,
    }


@router.get("/diagnostics")
async def ops_diagnostics_route(user=Depends(get_current_user)):
    from server import ops_diagnostics
    return await ops_diagnostics(user=user)


@router.get("/runtime")
async def ops_runtime_route(user=Depends(get_current_user)):
    from server import (
        APP_VERSION, START_TIME, get_git_info, get_file_version, app, db,
        get_user_settings, get_user_upstox_status, get_user_upstox_gateway
    )
    import os
    import time
    
    commit, branch, dirty = get_git_info()
    file_version = get_file_version()
    up_time = (datetime.now(timezone.utc) - START_TIME).total_seconds()
    
    settings = await get_user_settings(user["id"])
    paper_mode = bool(settings.get("paper_mode", True))
    
    # Check broker credentials config status
    upstox_status = await get_user_upstox_status(user["id"])
    broker_configured = bool(upstox_status.get("keys_saved"))
    
    # Check if live-auto is armed in DB
    arm_state = await db.live_arm_state.find_one({"user_id": user["id"]})
    live_auto_enabled = bool(arm_state and arm_state.get("armed"))
    
    # Check loop tasks status
    def get_task_status(task_name):
        task = getattr(app.state, task_name, None)
        if task is None:
            return "missing"
        elif task.done():
            if task.cancelled():
                return "cancelled"
            try:
                if task.exception():
                    return f"failed: {task.exception()}"
            except Exception:
                pass
            return "done"
        return "running"

    runner_status = get_task_status("runner_task")
    sig_status = get_task_status("signal_manager_task")
    
    # Check feed status
    feed_status = "disconnected"
    upstox_gw = await get_user_upstox_gateway(user["id"])
    if upstox_gw:
        gw_status = upstox_gw.status()
        if gw_status.get("feed_running"):
            feed_status = "connected"
            
    # Frontend version
    frontend_version = "10.0.0"
    try:
        from server import ROOT_DIR
        frontend_pkg = ROOT_DIR.parent / "frontend" / "package.json"
        if frontend_pkg.exists():
            import json
            with open(frontend_pkg, "r") as f:
                pkg_data = json.load(f)
                frontend_version = pkg_data.get("version", "10.0.0")
    except Exception:
        pass

    return {
        "backend_version": APP_VERSION,
        "file_version": file_version,
        "git_commit": commit,
        "git_branch": branch,
        "git_dirty": dirty,
        "start_time": START_TIME.isoformat(),
        "up_time_seconds": round(up_time, 2),
        "runtime_mode": os.environ.get("NODE_ENV", "production"),
        "paper_live_status": "PAPER" if paper_mode else "LIVE",
        "broker_configured_status": broker_configured,
        "live_auto_enabled": live_auto_enabled,
        "active_runner_task_status": runner_status,
        "active_signal_manager_task_status": sig_status,
        "feed_status": feed_status,
        "frontend_version": frontend_version,
        "frontend_build_metadata": None
    }



@router.post("/ticker/restart")
async def ops_restart_ticker(req: OpsActionReq = None, user=Depends(get_current_user), request: Request = None):
    from server import _start_user_upstox_ticker
    return await _start_user_upstox_ticker(user["id"])


@router.post("/orders/sync")
async def ops_sync_orders(req: OpsActionReq = None, user=Depends(get_current_user)):
    from server import (
        _ORDER_SYNC_CACHE, 
        _sync_upstox_order_statuses,
        _sync_strategy_positions_with_broker
    )
    
    _ORDER_SYNC_CACHE.pop(user["id"], None)
    upstox_sync = await _sync_upstox_order_statuses(user["id"], force=True)
    position_sync = await _sync_strategy_positions_with_broker(user["id"])
    return {"ok": True, "upstox_sync": upstox_sync, "position_sync": position_sync}


@router.get("/reconciliation/breaks")
async def ops_reconciliation_breaks(user=Depends(get_current_user)):
    return await _reconciliation_break_snapshot(user["id"])


@router.post("/reconciliation/resolve")
async def ops_resolve_reconciliation(req: OpsActionReq = None, user=Depends(get_current_user)):
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Access denied: Owner role required to resolve reconciliation breaks.")

    confirm = bool(req and req.confirm)
    phrase = str((req.note if req else "") or "").strip()
    snapshot = await _reconciliation_break_snapshot(user["id"])

    if not confirm or phrase != RECONCILIATION_RESOLVE_PHRASE:
        return {
            "ok": False,
            "dry_run": True,
            "required_note": RECONCILIATION_RESOLVE_PHRASE,
            "detail": "No data changed. Confirm only after checking the broker account manually.",
            "snapshot": snapshot,
        }

    blockers = snapshot["blockers"]
    if blockers["unknown_live_orders"] or blockers["reservations_needing_review"]:
        raise HTTPException(
            status_code=409,
            detail="Cannot clear reconciliation while live orders or exposure reservations still need review.",
        )

    now = datetime.now(timezone.utc).isoformat()
    res = await db["risk_state"].update_one(
        {"_id": f"position_reconciliation:{user['id']}"},
        {"$set": {
            "user_id": user["id"],
            "scope": "position_reconciliation",
            "mismatch_detected": False,
            "mismatches": [],
            "resolved_at": now,
            "resolved_by": user["id"],
            "resolution_note": phrase,
            "resolution_source": "ops_manual_broker_check",
        }},
        upsert=True,
    )
    await _append_ops_audit_event(
        user["id"],
        "RECONCILIATION_MANUALLY_RESOLVED",
        {
            "resolved_at": now,
            "matched": getattr(res, "matched_count", None),
            "modified": getattr(res, "modified_count", None),
            "previous_blockers": blockers,
        },
    )
    return {
        "ok": True,
        "resolved": True,
        "resolved_at": now,
        "detail": "Position reconciliation block cleared after owner-confirmed manual broker review.",
    }


@router.post("/auto-recover")
async def ops_auto_recover(req: OpsActionReq = None, user=Depends(get_current_user)):
    from server import (
        _ORDER_SYNC_CACHE,
        _sync_strategy_positions_with_broker,
        _sync_upstox_order_statuses,
        _start_user_upstox_ticker,
        ops_diagnostics
    )
    
    actions: List[Dict[str, Any]] = []
    _ORDER_SYNC_CACHE.pop(user["id"], None)
    actions.append({"name": "upstox_order_sync", "result": await _sync_upstox_order_statuses(user["id"], force=True)})
    actions.append({"name": "upstox_position_sync", "result": await _sync_strategy_positions_with_broker(user["id"])})
    actions.append({"name": "upstox_market_ticker", "result": await _start_user_upstox_ticker(user["id"])})

    diagnostics = await ops_diagnostics(user=user)
    return {"ok": True, "actions": actions, "recovery_plan": diagnostics.get("recovery_plan")}


@router.post("/emergency-stop")
async def ops_emergency_stop(req: OpsActionReq = None, user=Depends(get_current_user)):
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Access denied: Owner role required for emergency stop.")
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
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Access denied: Owner role required to bulk-enable strategies.")
    from server import option_ledger, _sync_option_ledger_strategy

    # Only re-enable strategies that were previously PAUSED — never touch draft or custom-stopped ones
    rows = await db.strategies.find({"user_id": user["id"], "status": "paused"}, {"_id": 0}).to_list(500)
    for row in rows:
        _sync_option_ledger_strategy(row)
        option_ledger.set_kill_switch(False, strategy_id=row["id"])
    res = await db.strategies.update_many(
        {"user_id": user["id"], "status": "paused"},
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


@router.post("/paper-orders/clear-stale")
async def ops_clear_stale_paper_orders(req: OpsActionReq = None, user=Depends(get_current_user)):
    now = datetime.now(timezone.utc).isoformat()
    from server import ORDER_ACTIVE_STATUSES, LEGACY_OPEN_STATUSES, ORDER_CANCELLED
    active_statuses = list(ORDER_ACTIVE_STATUSES | LEGACY_OPEN_STATUSES)
    
    res = await db.orders.update_many(
        {"user_id": user["id"], "mode": "paper", "status": {"$in": active_statuses}},
        {"$set": {
            "status": ORDER_CANCELLED,
            "legacy_status": "CANCELLED",
            "broker_status": "CANCELLED",
            "status_message": "Paper order cleared manually via Ops console.",
            "updated_at": now
        }}
    )
    
    await db.strategy_positions.update_many(
        {"user_id": user["id"], "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED"]}, "mode": "paper"},
        {"$set": {
            "status": "CANCELLED",
            "broker_status_message": "Paper position cleared manually via Ops console.",
            "updated_at": now
        },
         "$unset": {"active_instrument_key": "", "active_strategy_key": ""}}
    )
    
    await db.strategy_position_locks.delete_many({"user_id": user["id"]})
    
    return {"ok": True, "cleared_orders": res.modified_count}


@router.post("/accounts/reset-all-trading-state")
async def ops_reset_all_accounts_trading_state(req: OpsActionReq = None, user=Depends(get_current_user)):
    """Owner-only full app trading-state reset.

    This preserves login accounts and saved broker credentials, but resets every
    account to PAPER, disarms live trading, pauses strategies, and clears local
    trading state/projections. It intentionally requires an exact phrase.
    """
    if user.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Access denied: Owner role required.")
    confirm = bool(req and req.confirm)
    phrase = str((req.note if req else "") or "").strip()
    required_phrase = "RESET_ALL_ACCOUNTS_TO_PAPER"

    users = await db.users.find({}, {"_id": 0, "id": 1, "email": 1}).to_list(5000)
    user_ids = [u["id"] for u in users if u.get("id")]
    strategy_rows = await db.strategies.find({"user_id": {"$in": user_ids}}, {"_id": 0, "id": 1}).to_list(20000)
    strategy_ids = [s["id"] for s in strategy_rows if s.get("id")]

    trading_collections = {
        "orders": {"user_id": {"$in": user_ids}},
        "positions": {"user_id": {"$in": user_ids}},
        "strategy_positions": {"user_id": {"$in": user_ids}},
        "strategy_position_locks": {"user_id": {"$in": user_ids}},
        "signals": {"user_id": {"$in": user_ids}},
        "skipped_signals": {"user_id": {"$in": user_ids}},
        "paper_trading_history": {"user_id": {"$in": user_ids}},
        "trades": {"user_id": {"$in": user_ids}},
        "trade_fills": {"user_id": {"$in": user_ids}},
        "paper_wallets": {"user_id": {"$in": user_ids}},
        "risk_events": {"user_id": {"$in": user_ids}},
        "risk_reservations": {"user_id": {"$in": user_ids}},
        "risk_reservation_locks": {"user_id": {"$in": user_ids}},
        "order_events": {"user_id": {"$in": user_ids}},
        "outbox_events": {"user_id": {"$in": user_ids}},
        "broker_sync_state": {"user_id": {"$in": user_ids}},
        "live_arm_state": {"user_id": {"$in": user_ids}},
    }
    option_collections = {
        "option_open_positions": {"strategy_id": {"$in": strategy_ids}},
        "option_daily_pnl": {"strategy_id": {"$in": strategy_ids}},
        "option_trade_journal": {"strategy_id": {"$in": strategy_ids}},
    }
    risk_state_query = {
        "$or": [
            {"user_id": {"$in": user_ids}},
            {"_id": {"$in": [f"position_reconciliation:{uid}" for uid in user_ids]}},
        ]
    }

    preview = {}
    for coll, query in {**trading_collections, **option_collections, "risk_state": risk_state_query}.items():
        preview[coll] = await db[coll].count_documents(query)

    if not confirm or phrase != required_phrase:
        return {
            "ok": False,
            "dry_run": True,
            "required_note": required_phrase,
            "accounts": len(user_ids),
            "strategies": len(strategy_ids),
            "would_delete": preview,
            "detail": "No data changed. Send confirm=true and the required note to execute.",
        }

    now = datetime.now(timezone.utc).isoformat()
    purged = {}
    for coll, query in trading_collections.items():
        res = await db[coll].delete_many(query)
        purged[coll] = res.deleted_count
    for coll, query in option_collections.items():
        res = await db[coll].delete_many(query)
        purged[coll] = res.deleted_count
    risk_res = await db["risk_state"].delete_many(risk_state_query)
    purged["risk_state"] = risk_res.deleted_count

    users_res = await db.users.update_many(
        {"id": {"$in": user_ids}},
        {"$set": {
            "paper_mode": True,
            "data_broker": "upstox",
            "execution_broker": "upstox",
            "fallback_broker": "none",
            "reset_all_trading_state_at": now,
        }},
    )
    strategies_res = await db.strategies.update_many(
        {"user_id": {"$in": user_ids}},
        {"$set": {
            "mode": "paper",
            "status": "paused",
            "broker": "upstox",
            "halted": False,
            "is_halted": False,
            "last_error": "",
            "last_filter_reason": "Reset to PAPER by owner trading-state reset.",
            "reset_all_trading_state_at": now,
        },
         "$unset": {
             "halt_reason": "",
             "last_signal_validation": "",
             "last_fired_signal_date": "",
             "last_traded_symbol": "",
         }},
    )
    await db.option_strategy_states.update_many(
        {"strategy_id": {"$in": strategy_ids}},
        {"$set": {"state": "IDLE", "cooldown_until": None, "updated_at": now}},
    )

    try:
        from server import seed_default_strategies_for_user
        for uid in user_ids:
            await seed_default_strategies_for_user(uid)
    except Exception as exc:
        return {
            "ok": False,
            "partial": True,
            "detail": f"Trading state reset completed, but strategy reseeding failed: {exc}",
            "purged": purged,
            "updated_users": users_res.modified_count,
            "updated_strategies": strategies_res.modified_count,
        }

    return {
        "ok": True,
        "detail": "All accounts reset to PAPER trading state. Login accounts and saved Upstox credentials were preserved.",
        "accounts": len(user_ids),
        "strategies": len(strategy_ids),
        "updated_users": users_res.modified_count,
        "updated_strategies": strategies_res.modified_count,
        "purged": purged,
        "reset_at": now,
    }


@router.post("/positions/cleanup-orphans")
async def ops_cleanup_orphan_positions(req: OpsActionReq = None, user=Depends(get_current_user)):
    user_id = user["id"]
    
    # 1. Fetch all positions in db.positions
    positions = await db.positions.find({"user_id": user_id}).to_list(1000)
    
    # 2. Fetch active strategy positions
    active_sp = await db.strategy_positions.find({
        "user_id": user_id,
        "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]}
    }).to_list(1000)
    
    active_sp_symbols = {str(sp.get("symbol")).upper() for sp in active_sp if sp.get("symbol")}
    active_sp_strategy_ids = {str(sp.get("strategy_id")) for sp in active_sp if sp.get("strategy_id")}
    
    orphans = []
    for pos in positions:
        qty = int(pos.get("qty") or 0)
        if qty == 0:
            continue
        symbol = str(pos.get("symbol")).upper()
        strategy_id = pos.get("strategy_id")
        
        is_orphan = True
        if strategy_id and str(strategy_id) in active_sp_strategy_ids:
            is_orphan = False
        elif symbol in active_sp_symbols:
            is_orphan = False
            
        if is_orphan:
            orphans.append(pos)
            
    # Default is always dry-run. Cleanup requires owner role AND confirm=True
    confirm = bool(req and req.confirm)
    is_owner = str(user.get("role")).lower() == "owner"
    
    cleaned = 0
    report = [{"symbol": o["symbol"], "qty": o["qty"], "strategy_id": o.get("strategy_id")} for o in orphans]
    
    if confirm:
        if not is_owner:
            raise HTTPException(status_code=403, detail="Access denied: Owner role required for deletion.")
        for orphan in orphans:
            await db.positions.delete_one({"user_id": user_id, "symbol": orphan["symbol"]})
            cleaned += 1
            
    return {
        "ok": True,
        "orphans_found": len(orphans),
        "orphans": report,
        "cleaned_count": cleaned,
        "mode": "executed" if (confirm and is_owner) else "dry-run",
        "message": "Orphan positions cleaned up successfully." if (confirm and is_owner) else "Dry-run completed. No records deleted."
    }
