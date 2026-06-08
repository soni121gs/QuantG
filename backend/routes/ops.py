from __future__ import annotations

import os
import asyncio
import uuid
from datetime import datetime, timezone, timedelta
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


@router.get("/strategy-code-audit")
async def ops_strategy_code_audit(user=Depends(get_current_user)):
    from core.strategy_brain_schema import calculate_code_hash
    
    # Fetch all strategies for this user
    db_strategies = await db.strategies.find({"user_id": user["id"]}).to_list(1000)
    
    # Group by code hash to identify duplicate code groups
    hash_groups = {}
    for s in db_strategies:
        code = s.get("python_code") or ""
        ch = calculate_code_hash(code)
        hash_groups.setdefault(ch, []).append(s)
        
    duplicate_groups = []
    for ch, group_strats in hash_groups.items():
        if len(group_strats) > 1:
            duplicate_groups.append({
                "code_hash": ch,
                "strategy_ids": [s.get("id") or str(s.get("_id")) for s in group_strats],
                "names": [s.get("name") for s in group_strats],
                "count": len(group_strats)
            })
            
    # Process each strategy
    strategies_report = []
    live_count = 0
    for s in db_strategies:
        code = s.get("python_code") or ""
        ch = calculate_code_hash(code)
        is_dup = len(hash_groups[ch]) > 1
        
        status = s.get("status")
        if status == "live":
            live_count += 1
            
        underlying = s.get("underlying") or s.get("visual_config", {}).get("symbol") or s.get("visual_config", {}).get("options", {}).get("underlying") or ""
        underlying = str(underlying).upper()
        
        instrument_group = s.get("instrument_group") or s.get("visual_config", {}).get("exchange")
        if instrument_group:
            instrument_group = str(instrument_group).upper()
        else:
            instrument_group = "BFO" if underlying == "SENSEX" else "NFO"
            
        risk_style = s.get("risk_style") or s.get("visual_config", {}).get("risk", {}).get("risk_style") or s.get("risk", {}).get("risk_style")
        exit_mode = s.get("exit_mode") or s.get("risk", {}).get("exit_mode") or s.get("visual_config", {}).get("risk", {}).get("exit_mode") or "signal_or_tp_sl_trailing"
        
        strategies_report.append({
            "strategy_id": s.get("id") or str(s.get("_id")),
            "name": s.get("name"),
            "status": status,
            "mode": s.get("mode") or "paper",
            "broker": s.get("broker") or "upstox",
            "underlying": underlying,
            "instrument_group": instrument_group,
            "code_hash": ch,
            "duplicate_code_group": ch if is_dup else None,
            "risk_style": risk_style,
            "exit_mode": exit_mode,
            "default_strategy_version": s.get("default_strategy_version"),
            "strategy_logic_version": s.get("strategy_logic_version"),
            "signal_schema_status": s.get("signal_schema_status")
        })
        
    return {
        "ok": True,
        "total_strategies": len(db_strategies),
        "live_strategies": live_count,
        "duplicate_code_groups": duplicate_groups,
        "strategies": strategies_report
    }


@router.get("/default-strategy-catalog-audit")
async def ops_default_strategy_catalog_audit(user=Depends(get_current_user)):
    from server import DEFAULT_OPTION_STRATEGIES
    from core.strategy_brain_schema import calculate_code_hash
    
    # Group default strategies by code hash
    hash_groups = {}
    for t in DEFAULT_OPTION_STRATEGIES:
        code = t.get("python_code") or ""
        ch = calculate_code_hash(code)
        hash_groups.setdefault(ch, []).append(t.get("name"))
        
    strategies_report = []
    any_shares = False
    for t in DEFAULT_OPTION_STRATEGIES:
        code = t.get("python_code") or ""
        ch = calculate_code_hash(code)
        shares = len(hash_groups[ch]) > 1
        if shares:
            any_shares = True
            
        underlying = t.get("underlying") or t.get("visual_config", {}).get("options", {}).get("underlying") or ""
        underlying = str(underlying).upper()
        
        instrument_group = t.get("instrument_group") or t.get("visual_config", {}).get("exchange")
        if instrument_group:
            instrument_group = str(instrument_group).upper()
        else:
            instrument_group = "BFO" if underlying == "SENSEX" else "NFO"
            
        risk_style = t.get("risk_style") or t.get("risk", {}).get("risk_style") or t.get("visual_config", {}).get("risk", {}).get("risk_style")
        exit_mode = t.get("exit_mode") or t.get("risk", {}).get("exit_mode") or t.get("visual_config", {}).get("risk", {}).get("exit_mode") or "signal_or_tp_sl_trailing"
        
        strategies_report.append({
            "name": t.get("name"),
            "underlying": underlying,
            "instrument_group": instrument_group,
            "required_capital": t.get("required_capital"),
            "risk_style": risk_style,
            "exit_mode": exit_mode,
            "market_suitability": t.get("market_suitability"),
            "code_hash": ch,
            "shares_code": shares
        })
        
    mcx_excluded = all(
        str(t.get("instrument_group") or "").upper() != "MCX"
        and "CRUDE" not in str(t.get("name") or "").upper()
        and "NATURAL GAS" not in str(t.get("name") or "").upper()
        for t in DEFAULT_OPTION_STRATEGIES
    )
    
    return {
        "ok": True,
        "total_default_strategies": len(DEFAULT_OPTION_STRATEGIES),
        "names": [t.get("name") for t in DEFAULT_OPTION_STRATEGIES],
        "mcx_crude_naturalgas_excluded": mcx_excluded,
        "shares_code_with_another": any_shares,
        "strategies": strategies_report
    }


@router.post("/v13/strategy-brain/dry-run")
async def ops_v13_strategy_brain_dry_run(user=Depends(get_current_user)):
    from server import DEFAULT_OPTION_STRATEGIES
    from core.strategy_brain_schema import calculate_code_hash
    
    db_strategies = await db.strategies.find({"user_id": user["id"]}).to_list(1000)
    db_names = {s["name"] for s in db_strategies if s.get("name")}
    
    # 1. strategies_to_insert: names of default strategies not in DB
    strategies_to_insert = [
        t["name"] for t in DEFAULT_OPTION_STRATEGIES if t["name"] not in db_names
    ]
    
    # 2. strategies_to_update_by_name: names of default strategies that are in DB
    strategies_to_update_by_name = [
        t["name"] for t in DEFAULT_OPTION_STRATEGIES if t["name"] in db_names
    ]
    
    # 3. duplicate_strategy_names: names that appear > 1 time in DB for this user
    name_counts = {}
    for s in db_strategies:
        name = s.get("name")
        if name:
            name_counts[name] = name_counts.get(name, 0) + 1
    duplicate_strategy_names = [name for name, count in name_counts.items() if count > 1]
    
    # 4. duplicate_code_hashes: code hashes that appear > 1 time in DB for this user
    hash_counts = {}
    for s in db_strategies:
        code = s.get("python_code") or ""
        ch = calculate_code_hash(code)
        hash_counts[ch] = hash_counts.get(ch, 0) + 1
    duplicate_code_hashes = [ch for ch, count in hash_counts.items() if count > 1]
    
    # 5. old_default_versions: mapping of default strategy name to its default_strategy_version
    default_names = {t["name"] for t in DEFAULT_OPTION_STRATEGIES}
    old_default_versions = {
        s["name"]: s.get("default_strategy_version")
        for s in db_strategies
        if s.get("name") in default_names and s.get("default_strategy_version")
    }
    
    # 6. missing_v13_signal_metadata: list of names in DB missing signal_schema_status == "V13_SIGNAL"
    missing_v13_signal_metadata = [
        s["name"] for s in db_strategies
        if s.get("signal_schema_status") != "V13_SIGNAL"
    ]
    
    # 7. strategies_that_use_signal_only_exit: list of strategy names using exit_mode == "signal_only"
    strategies_that_use_signal_only_exit = []
    for s in db_strategies:
        exit_mode = s.get("exit_mode") or s.get("risk", {}).get("exit_mode") or s.get("visual_config", {}).get("risk", {}).get("exit_mode")
        if exit_mode == "signal_only":
            strategies_that_use_signal_only_exit.append(s["name"])
            
    # 8. MCX_removed_confirmed: confirm no MCX/CRUDE/NATURALGAS strategies are in the catalog
    mcx_removed_confirmed = all(
        str(t.get("instrument_group") or "").upper() != "MCX"
        and "CRUDE" not in str(t.get("name") or "").upper()
        and "NATURAL GAS" not in str(t.get("name") or "").upper()
        for t in DEFAULT_OPTION_STRATEGIES
    )
    
    return {
        "ok": True,
        "strategies_to_insert": strategies_to_insert,
        "strategies_to_update_by_name": strategies_to_update_by_name,
        "duplicate_strategy_names": duplicate_strategy_names,
        "duplicate_code_hashes": duplicate_code_hashes,
        "old_default_versions": old_default_versions,
        "missing_v13_signal_metadata": missing_v13_signal_metadata,
        "strategies_that_use_signal_only_exit": strategies_that_use_signal_only_exit,
        "MCX_removed_confirmed": mcx_removed_confirmed
    }


@router.get("/r-exit-status")
async def ops_r_exit_status(user=Depends(get_current_user)):
    from server import _current_ltp_for_symbol
    user_id = user["id"]
    
    active_positions = await db.strategy_positions.find({
        "user_id": user_id,
        "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}
    }, {"_id": 0}).to_list(1000)
    
    positions_report = []
    for pos in active_positions:
        symbol = pos.get("trading_symbol") or pos.get("symbol")
        if not symbol:
            continue
            
        exchange = pos.get("exchange") or ("NFO" if symbol.endswith(("CE", "PE")) else "NSE")
        is_option_buy = (
            pos.get("position_side") == "LONG"
            and (
                exchange in {"NFO", "BFO"}
                or symbol.endswith(("CE", "PE"))
                or pos.get("option_type") in {"CE", "PE"}
            )
        )
        if not is_option_buy:
            continue
            
        current_price = await _current_ltp_for_symbol(user_id, symbol, exchange)
        
        strat = await db.strategies.find_one({"id": pos.get("strategy_id"), "user_id": user_id})
        strategy_name = strat.get("name") if strat else pos.get("strategy_id")
        
        entry_time_str = pos.get("entry_time") or pos.get("created_at")
        elapsed_minutes = 0.0
        if entry_time_str:
            try:
                entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                elapsed_minutes = round((datetime.now(timezone.utc) - entry_dt.astimezone(timezone.utc)).total_seconds() / 60.0, 2)
            except Exception:
                pass
                
        positions_report.append({
            "strategy_id": pos.get("strategy_id"),
            "strategy_name": strategy_name,
            "symbol": symbol,
            "position_side": pos.get("position_side"),
            "status": pos.get("status"),
            "ltp": current_price,
            "average_buy_price": pos.get("average_buy_price"),
            "r_initial_risk_amount": pos.get("r_initial_risk_amount"),
            "r_stop_loss_price": pos.get("r_stop_loss_price"),
            "r_take_profit_price": pos.get("r_take_profit_price"),
            "r_current_R": pos.get("r_current_R"),
            "r_max_R_seen": pos.get("r_max_R_seen"),
            "r_trailing_active": pos.get("r_trailing_active"),
            "r_trailing_stop_price": pos.get("r_trailing_stop_price"),
            "best_price_seen": pos.get("best_price_seen"),
            "elapsed_minutes": elapsed_minutes,
            "max_hold_minutes": pos.get("max_hold_minutes"),
        })
        
    return {
        "ok": True,
        "positions": positions_report
    }


@router.get("/option-selector-status")
async def option_selector_status(user=Depends(get_current_user)):
    """
    Returns the last 100 OptionSelector v2 decisions for the authenticated user.

    Each record contains:
        strategy_id, underlying, direction, preference, selected_strike_mode,
        quality_score, reason_code, mode, created_at,
        selected_contract.instrument_key / tradingsymbol / strike / ltp
    """
    user_id = user["id"]
    raw_rows = await db.option_selector_decisions.find(
        {"user_id": user_id},
        {"_id": 0},
    ).sort("created_at", -1).to_list(100)

    rows = []
    for row in raw_rows:
        contract = row.get("selected_contract") or {}
        rows.append({
            "strategy_id": row.get("strategy_id"),
            "underlying": row.get("underlying"),
            "direction": row.get("direction"),
            "preference": row.get("preference"),
            "selected_strike_mode": row.get("selected_strike_mode"),
            "quality_score": row.get("quality_score"),
            "reason_code": row.get("reason_code"),
            "mode": row.get("mode"),
            "created_at": row.get("created_at"),
            "instrument_key": contract.get("instrument_key"),
            "tradingsymbol": contract.get("tradingsymbol"),
            "strike": contract.get("strike"),
            "ltp": contract.get("ltp"),
        })

    blocked = [r for r in rows if r["reason_code"] != "SELECTED"]
    selected = [r for r in rows if r["reason_code"] == "SELECTED"]

    return {
        "ok": True,
        "total": len(rows),
        "selected_count": len(selected),
        "blocked_count": len(blocked),
        "decisions": rows,
    }


@router.get("/signal-priority-status")
async def signal_priority_status(user=Depends(get_current_user)):
    """
    Returns the last 100 ConflictResolver priority decisions for the authenticated user.

    Each record contains:
        strategy_name, symbol_group, action, confidence, option_quality_score,
        target_R, priority_score, decision (APPROVED/SKIPPED), reason_code, created_at

    user_id is intentionally omitted from the response.
    """
    user_id = user["id"]
    raw_rows = await db.signal_priority_decisions.find(
        {"user_id": user_id},
        {"_id": 0, "user_id": 0},   # exclude user_id from output
    ).sort("created_at", -1).to_list(100)

    approved_count = sum(1 for r in raw_rows if r.get("decision") == "APPROVED")
    skipped_count = sum(1 for r in raw_rows if r.get("decision") == "SKIPPED")

    return {
        "ok": True,
        "total": len(raw_rows),
        "approved_count": approved_count,
        "skipped_count": skipped_count,
        "decisions": raw_rows,
    }


@router.get("/live-readiness")
async def ops_live_readiness(user=Depends(get_current_user)):
    """
    Comprehensive live-readiness check for the QuantG ops dashboard.

    Returns status=READY only when all critical checks pass.
    Informational fields (counts, versions, feature flags) are always included.

    Critical checks (any failure → NOT_READY):
      1. CORE_ENGINE_LIVE_ENABLED env flag
      2. User armed + global_live_enabled
      3. Global kill-switch inactive
      4. Upstox token valid (not mock)
      5. Market data feed connected
    """
    from server import (
        APP_VERSION, get_git_info, get_user_upstox_status, get_user_upstox_gateway,
    )
    import os as _os

    user_id = user["id"]
    now_iso = datetime.now(timezone.utc).isoformat()
    reasons: list = []

    # ── git / version ──────────────────────────────────────────────────────
    try:
        commit, branch, _ = get_git_info()
    except Exception:
        commit, branch = "unknown", "unknown"

    # ── 1. CORE_ENGINE_LIVE_ENABLED ────────────────────────────────────────
    live_env = _os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true"
    if not live_env:
        reasons.append("CORE_ENGINE_LIVE_ENABLED not set to true")

    # ── 2. Arm state ───────────────────────────────────────────────────────
    arm_doc = await db.live_arm_state.find_one({"user_id": user_id})
    armed = bool(arm_doc and arm_doc.get("armed"))
    global_live_enabled = bool(arm_doc and arm_doc.get("global_live_enabled"))
    if not (armed and global_live_enabled):
        reasons.append("System not armed (armed=False or global_live_enabled=False)")

    # ── 3. Kill switch ─────────────────────────────────────────────────────
    ks_doc = await db.risk_state.find_one({"_id": "global_kill_switch"})
    kill_active = bool(ks_doc and ks_doc.get("active"))
    if kill_active:
        reasons.append("Global kill-switch is active")

    # ── 4. Upstox token valid ──────────────────────────────────────────────
    try:
        upstox_status = await get_user_upstox_status(user_id)
    except Exception:
        upstox_status = {}
    token_valid = bool(upstox_status.get("connected") and upstox_status.get("token_valid"))
    # Reject mock tokens explicitly
    broker_keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "upstox"})
    mock_token = bool(broker_keys and str(broker_keys.get("access_token", "")).startswith("mock_"))
    if mock_token:
        token_valid = False
    if not token_valid:
        reasons.append("Upstox token missing, invalid, or is a mock token")

    # ── 5. Feed connected ──────────────────────────────────────────────────
    feed_connected = False
    latest_tick_age_seconds = None
    try:
        upstox_gw = await get_user_upstox_gateway(user_id)
        if upstox_gw:
            gw_status = upstox_gw.status()
            feed_connected = bool(gw_status.get("feed_running") or gw_status.get("ws_running"))
            last_tick = gw_status.get("last_tick_time") or gw_status.get("last_tick_at")
            if last_tick:
                try:
                    lt = datetime.fromisoformat(str(last_tick).replace("Z", "+00:00"))
                    lt_utc = lt if lt.tzinfo else lt.replace(tzinfo=timezone.utc)
                    latest_tick_age_seconds = round(
                        (datetime.now(timezone.utc) - lt_utc.astimezone(timezone.utc)).total_seconds(), 1
                    )
                except Exception:
                    pass
    except Exception:
        pass
    if not feed_connected:
        reasons.append("Upstox market data feed not connected")

    # ── Informational counts ───────────────────────────────────────────────
    live_strategy_count = await db.strategies.count_documents(
        {"user_id": user_id, "mode": "live", "status": "live"}
    )
    open_positions_count = await db.strategy_positions.count_documents(
        {"user_id": user_id, "status": {"$in": ["OPEN", "FILLED", "PENDING_BROKER", "EXITING"]}}
    )
    pending_orders_count = await db.orders.count_documents(
        {"user_id": user_id, "mode": "live", "status": {"$in": ["PLACED", "PENDING_BROKER", "OPEN"]}}
    )
    unknown_orders_count = await db.orders.count_documents(
        {"user_id": user_id, "status": "UNKNOWN_NEEDS_REVIEW"}
    )

    # ── Feature-active flags (based on recent decisions) ───────────────────
    cutoff_1h = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    option_selector_active = bool(
        await db.option_selector_decisions.find_one(
            {"user_id": user_id, "created_at": {"$gte": cutoff_1h}}
        )
    )
    r_exit_active = bool(
        await db.strategy_positions.find_one(
            {"user_id": user_id, "r_stop_loss_price": {"$exists": True},
             "status": {"$in": ["OPEN", "FILLED", "PENDING_BROKER"]}}
        )
    )
    signal_priority_active = bool(
        await db.signal_priority_decisions.find_one(
            {"user_id": user_id, "created_at": {"$gte": cutoff_1h}}
        )
    )

    # ── Default strategy version (from most recent live strategy) ──────────
    recent_strat = await db.strategies.find_one(
        {"user_id": user_id, "mode": "live"},
        sort=[("updated_at", -1)],
    )
    default_strategy_version = (
        (recent_strat or {}).get("default_strategy_version")
        or (recent_strat or {}).get("strategy_version")
        or "v13-live-brain-r1"
    )

    status = "READY" if not reasons else "NOT_READY"

    return {
        "status": status,
        "reasons": reasons,
        "app_version": APP_VERSION,
        "git_commit": commit,
        "git_branch": branch,
        "CORE_ENGINE_LIVE_ENABLED": live_env,
        "arm_state": {
            "armed": armed,
            "global_live_enabled": global_live_enabled,
        },
        "kill_switch_active": kill_active,
        "upstox_token_valid": token_valid,
        "feed_connected": feed_connected,
        "latest_tick_age_seconds": latest_tick_age_seconds,
        "live_strategy_count": live_strategy_count,
        "open_positions_count": open_positions_count,
        "pending_orders_count": pending_orders_count,
        "unknown_orders_count": unknown_orders_count,
        "default_strategy_version": default_strategy_version,
        "option_selector_active": option_selector_active,
        "r_exit_active": r_exit_active,
        "signal_priority_active": signal_priority_active,
        "timestamp": now_iso,
    }
