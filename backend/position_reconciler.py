"""Position reconciliation and automatic self-healing engine."""
from __future__ import annotations

import logging
import uuid
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pymongo import ReturnDocument

# We import db from core
from core import db

logger = logging.getLogger("quantg.position_reconciler")

async def create_manual_recovery_strategy_if_missing(user_id: str, paper_mode: bool) -> str:
    """Ensure the MANUAL_RECOVERY strategy exists for the user."""
    strat = await db.strategies.find_one({"id": "manual_recovery", "user_id": user_id})
    if not strat:
        now = datetime.now(timezone.utc).isoformat()
        recovery_strat = {
            "id": "manual_recovery",
            "user_id": user_id,
            "name": "MANUAL_RECOVERY",
            "status": "live",
            "broker": "upstox",
            "mode": "paper" if paper_mode else "live",
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "visual_config": {
                "options": {"enabled": True, "underlying": "NIFTY"},
                "risk": {
                    "stop_loss_pct": 0.0,
                    "take_profit_pct": 0.0,
                    "trail_trigger_pct": 0.0,
                    "trail_step_pct": 0.0,
                    "trailing_sl_enabled": False,
                    "daily_loss_limit": 0.0,
                    "required_capital": 0.0,
                    "cooldown_minutes": 0,
                    "max_trades_day": 999,
                }
            },
            "created_at": now,
            "updated_at": now,
        }
        try:
            await db.strategies.insert_one(recovery_strat)
            logger.info("Created MANUAL_RECOVERY strategy for user %s", user_id)
        except Exception as e:
            logger.warning("Failed to create MANUAL_RECOVERY strategy: %s", e)
    return "manual_recovery"

def calculate_protection_for_position(average_buy_price: float, side: str, strategy: dict) -> Dict[str, Optional[float]]:
    """Restore original strategy risk settings if available. Do NOT invent risk values if missing."""
    entry = float(average_buy_price or 0)
    if entry <= 0:
        return {"stop_loss": None, "take_profit": None, "protection_status": "UNPROTECTED"}

    # Attempt to load risk config
    visual_config = strategy.get("visual_config") or {}
    risk = visual_config.get("risk") or {}
    
    stop_pct = risk.get("stop_loss_pct") or risk.get("stoploss_pct") or risk.get("stop_pct")
    target_pct = risk.get("take_profit_pct") or risk.get("target_pct") or risk.get("tp_pct")
    
    stop_price = risk.get("stoploss_price") or risk.get("stop_loss")
    target_price = risk.get("target_price") or risk.get("take_profit")
    
    side_upper = str(side or "LONG").upper()
    
    # Do NOT invent risk values (like default 8% or 20%) if they are not in the strategy config
    stop_val = None
    target_val = None
    
    if stop_price not in (None, "", 0):
        stop_val = round(float(stop_price), 2)
    elif stop_pct not in (None, "", 0):
        stop_pct = float(stop_pct)
        # Handle percentage format (e.g. 0.08 vs 8%)
        if stop_pct < 1.0:
            stop_pct *= 100.0
        stop_val = entry * (1 - stop_pct / 100) if side_upper != "SHORT" else entry * (1 + stop_pct / 100)
        stop_val = round(float(stop_val), 2)
        
    if target_price not in (None, "", 0):
        target_val = round(float(target_price), 2)
    elif target_pct not in (None, "", 0):
        target_pct = float(target_pct)
        if target_pct < 1.0:
            target_pct *= 100.0
        target_val = entry * (1 + target_pct / 100) if side_upper != "SHORT" else entry * (1 - target_pct / 100)
        target_val = round(float(target_val), 2)
        
    protection_status = "PROTECTED" if (stop_val is not None and target_val is not None) else "UNPROTECTED"
    
    return {
        "stop_loss": stop_val,
        "take_profit": target_val,
        "protection_status": protection_status
    }

async def reconcile_and_repair_positions(user_id: str) -> Dict[str, int]:
    """Compare broker, paper broker, strategy ledger, and database states to repair mismatches."""
    from server import get_user_settings, _fetch_broker_positions_for_user, option_ledger
    
    settings = await get_user_settings(user_id)
    paper_mode = bool(settings.get("paper_mode", True))
    
    # 1. Ensure MANUAL_RECOVERY strategy exists
    await create_manual_recovery_strategy_if_missing(user_id, paper_mode)
    
    # 2. Fetch active broker/paper positions
    user_doc = {"id": user_id}
    broker_positions = await _fetch_broker_positions_for_user(user_doc, settings)
    
    # Normalize broker positions
    broker_active = {}
    for bp in broker_positions:
        symbol = str(bp.get("symbol") or "").upper()
        qty = int(bp.get("qty") or bp.get("quantity") or 0)
        if qty != 0:
            broker_active[symbol] = {
                "symbol": symbol,
                "qty": qty,
                "avg_price": float(bp.get("avg_price") or bp.get("average_price") or 0),
                "exchange": bp.get("exchange") or "NSE",
                "strategy_id": bp.get("strategy_id"),
            }

    # 3. Fetch active strategy positions (Ledger)
    active_ledger_rows = await db.strategy_positions.find({
        "user_id": user_id,
        "status": {"$in": ["PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]}
    }).to_list(1000)
    
    ledger_active = {str(row.get("symbol") or "").upper(): row for row in active_ledger_rows if row.get("symbol")}

    stats = {
        "orphans_healed": 0,
        "stale_closed": 0,
        "quantities_aligned": 0,
        "protections_verified": 0,
    }
    
    now = datetime.now(timezone.utc).isoformat()
    
    # 4. Step A: Process Orphans (Broker position exists, but no matching ledger row)
    for symbol, bp in broker_active.items():
        qty = bp["qty"]
        avg_price = bp["avg_price"]
        
        if symbol not in ledger_active:
            # Orphan Position found!
            logger.warning("RECONCILIATION: Orphan position detected for %s with qty %s", symbol, qty)
            
            # Find owner strategy ID
            resolved_strat_id = None
            recent_order = await db.orders.find_one(
                {"user_id": user_id, "symbol": symbol, "strategy_id": {"$exists": True, "$ne": None}},
                sort=[("created_at", -1)]
            )
            if recent_order:
                candidate_id = recent_order.get("strategy_id")
                strat = await db.strategies.find_one({"id": candidate_id, "user_id": user_id})
                if strat:
                    resolved_strat_id = candidate_id
            
            # If still not resolved, assign to MANUAL_RECOVERY
            if not resolved_strat_id:
                resolved_strat_id = "manual_recovery"
                logger.warning("RECONCILIATION: Orphan %s is unrecoverable. Assigning to MANUAL_RECOVERY.", symbol)
            
            # Retrieve strategy details
            strat_doc = await db.strategies.find_one({"id": resolved_strat_id, "user_id": user_id})
            strat_name = strat_doc.get("name") if strat_doc else resolved_strat_id
            
            # Rebuild protection without inventing SL/TP
            side = "LONG" if qty > 0 else "SHORT"
            prot = calculate_protection_for_position(avg_price, side, strat_doc or {})
            if prot["protection_status"] == "UNPROTECTED":
                logger.warning("PROTECTION WARNING: Restoring orphan %s as UNPROTECTED (no strategy risk settings available).", symbol)
            
            # Rebuild strategy position ledger row
            restored_position_id = str(uuid.uuid4())
            new_ledger_row = {
                "id": restored_position_id,
                "user_id": user_id,
                "strategy_id": resolved_strat_id,
                "strategy_name": strat_name,
                "instrument_key": f"{bp['exchange']}:{symbol}",
                "symbol_group": symbol,
                "instrument_token": bp.get("instrument_token"),
                "trading_symbol": symbol,
                "symbol": symbol,
                "exchange": bp["exchange"],
                "quantity": abs(qty),
                "open_quantity": abs(qty),
                "average_buy_price": avg_price,
                "position_side": side,
                "entry_time": now,
                "status": "OPEN",
                "tp_sl_tsl_config": {
                    "stoploss_price": prot["stop_loss"],
                    "stop_loss": prot["stop_loss"],
                    "target_price": prot["take_profit"],
                    "take_profit": prot["take_profit"],
                    "protection_status": prot["protection_status"],
                },
                "source": "reconciliation-repair",
                "created_at": now,
                "updated_at": now,
            }
            
            await db.strategy_positions.insert_one(new_ledger_row)
            
            # Update local paper positions if in paper mode
            if paper_mode:
                await db.positions.update_many(
                    {"user_id": user_id, "symbol": symbol},
                    {"$set": {"strategy_id": resolved_strat_id, "updated_at": now}}
                )
                
            # If options strategy, rebuild SQLite ledger state
            if strat_doc and ((strat_doc.get("visual_config") or {}).get("options") or {}).get("enabled"):
                try:
                    option_ledger.upsert_strategy_state(resolved_strat_id)
                    option_ledger.confirm_position_fill(
                        strategy_id=resolved_strat_id,
                        entry_price=avg_price,
                        quantity=abs(qty),
                        position_side=side,
                        create_if_missing=True
                    )
                except Exception as e:
                    logger.warning("Failed to sync options ledger state: %s", e)
            
            stats["orphans_healed"] += 1
            # Add to local active ledger so it won't be processed as stale later
            ledger_active[symbol] = new_ledger_row

    # 5. Step B: Process Stale Positions (Ledger active but broker qty is 0)
    for symbol, ledger_pos in ledger_active.items():
        if symbol not in broker_active:
            # Stale position in Ledger!
            logger.warning("RECONCILIATION: Stale strategy position detected for %s in ledger. Closing position.", symbol)
            
            exit_price = float(ledger_pos.get("average_buy_price") or 0)
            avg_buy = float(ledger_pos.get("average_buy_price") or 0)
            qty = int(ledger_pos.get("open_quantity") or ledger_pos.get("quantity") or 0)
            side = ledger_pos.get("position_side") or "LONG"
            
            pnl = 0.0
            
            await db.strategy_positions.update_one(
                {"id": ledger_pos["id"], "user_id": user_id},
                {"$set": {
                    "status": "CLOSED",
                    "open_quantity": 0,
                    "exit_price": exit_price,
                    "realised_pnl": pnl,
                    "exit_reason": "reconciliation-repair",
                    "closed_at": now,
                    "updated_at": now,
                }, "$unset": {"active_instrument_key": "", "active_strategy_key": ""}}
            )
            
            # Release strategy locks
            await db.strategy_position_locks.delete_many({
                "user_id": user_id,
                "strategy_id": ledger_pos["strategy_id"]
            })
            
            # Close in options ledger if it's there
            try:
                option_ledger.close_position(ledger_pos["strategy_id"], exit_price, "reconciliation-repair")
            except Exception:
                pass
                
            stats["stale_closed"] += 1

    # 6. Step C: Quantity and Protection Alignment
    for symbol, ledger_pos in ledger_active.items():
        if symbol in broker_active:
            bp = broker_active[symbol]
            broker_qty = abs(bp["qty"])
            ledger_qty = ledger_pos.get("open_quantity") or ledger_pos.get("quantity") or 0
            
            # Quantity misalignment
            if broker_qty != ledger_qty:
                logger.warning("RECONCILIATION: Quantity mismatch for %s. Broker: %s, Ledger: %s. Re-aligning.", symbol, broker_qty, ledger_qty)
                await db.strategy_positions.update_one(
                    {"id": ledger_pos["id"], "user_id": user_id},
                    {"$set": {"open_quantity": broker_qty, "quantity": broker_qty, "updated_at": now}}
                )
                stats["quantities_aligned"] += 1
            
            # Verify SL/TP protection is compliant (original config if available, UNPROTECTED warning if not)
            tp_sl_cfg = ledger_pos.get("tp_sl_tsl_config") or {}
            has_sl = tp_sl_cfg.get("stop_loss") is not None or tp_sl_cfg.get("stoploss_price") is not None
            has_tp = tp_sl_cfg.get("take_profit") is not None or tp_sl_cfg.get("target_price") is not None
            
            if not has_sl or not has_tp:
                strat_doc = await db.strategies.find_one({"id": ledger_pos["strategy_id"], "user_id": user_id})
                side = ledger_pos.get("position_side") or ("LONG" if bp["qty"] > 0 else "SHORT")
                avg_price = ledger_pos.get("average_buy_price") or bp["avg_price"]
                prot = calculate_protection_for_position(avg_price, side, strat_doc or {})
                
                if prot["protection_status"] == "UNPROTECTED":
                    logger.warning("PROTECTION WARNING: Active position %s remains UNPROTECTED (no strategy risk settings available).", symbol)
                
                await db.strategy_positions.update_one(
                    {"id": ledger_pos["id"], "user_id": user_id},
                    {"$set": {
                        "tp_sl_tsl_config.stoploss_price": prot["stop_loss"],
                        "tp_sl_tsl_config.stop_loss": prot["stop_loss"],
                        "tp_sl_tsl_config.target_price": prot["take_profit"],
                        "tp_sl_tsl_config.take_profit": prot["take_profit"],
                        "tp_sl_tsl_config.protection_status": prot["protection_status"],
                        "updated_at": now
                    }}
                )
                stats["protections_verified"] += 1
                
    return stats
