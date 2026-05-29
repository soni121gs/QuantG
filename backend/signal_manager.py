"""Signal Manager and Conflict Resolver.

Centralizes signal queuing, validates strategy-level limits (cooldown, daily max),
and applies multi-strategy conflict resolution rules before execution.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple
from pymongo import ReturnDocument

logger = logging.getLogger("quantg.signal_manager")

TICK_SECONDS = int(os.environ.get("SIGNAL_MANAGER_TICK_SECONDS", "2"))
LOCK_TTL_SECONDS = 90
LOCK_ID = "signal_manager"
POD_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class ConflictResolver:
    """Evaluates pending signals for directional conflicts and duplicate prevention."""

    @staticmethod
    def resolve(
        pending_signals: List[Dict[str, Any]],
        active_positions: List[Dict[str, Any]],
        one_active_position_per_symbol_group: bool = True
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Processes a list of PENDING signals for a user sweep.

        Returns a tuple: (approved_signals, rejected_or_filtered_signals)
        """
        approved: List[Dict[str, Any]] = []
        rejected_or_filtered: List[Dict[str, Any]] = []

        # Group pending signals by underlying (symbol)
        by_underlying: Dict[str, List[Dict[str, Any]]] = {}
        for sig in pending_signals:
            und = str(sig.get("symbol") or "").upper()
            by_underlying.setdefault(und, []).append(sig)

        # Map active positions to their underlying symbol group
        active_groups = set()
        for pos in active_positions:
            # Standardize symbol group identification
            group = str(pos.get("symbol_group") or pos.get("symbol") or "").upper()
            if group:
                active_groups.add(group)

        for und, sigs in by_underlying.items():
            # 1. Check One-Active-Position-Per-Symbol-Group rule
            if one_active_position_per_symbol_group and und in active_groups:
                for sig in sigs:
                    if sig.get("action") == "BUY":
                        sig["status"] = "BLOCKED"
                        sig["rejection_reason"] = "symbol-group-active-position-exists"
                        rejected_or_filtered.append(sig)
                    else:
                        # Exits (SELL) are allowed to bypass the group-level lockout
                        approved.append(sig)
                continue

            # 2. Check CE/PE Clashing
            ce_buys: List[Dict[str, Any]] = []
            pe_buys: List[Dict[str, Any]] = []
            exits: List[Dict[str, Any]] = []

            for sig in sigs:
                action = str(sig.get("action") or "").upper()
                if action == "SELL":
                    exits.append(sig)
                    continue

                # It's a BUY signal
                opt_cfg = sig.get("visual_config", {}).get("options") or {}
                strike_mode = str(opt_cfg.get("strike_mode") or "").upper()
                opt_type = str(sig.get("option_type") or "").upper()
                
                # Resolve option type from config or direct field
                if not opt_type:
                    if "BUY" in strike_mode:
                        opt_type = "CE" if action == "BUY" else "PE"
                    else:
                        opt_type = "PE" if action == "BUY" else "CE"

                if opt_type == "CE":
                    ce_buys.append(sig)
                elif opt_type == "PE":
                    pe_buys.append(sig)
                else:
                    # Non-option asset
                    approved.append(sig)

            # Exits are always approved to avoid position lockups
            approved.extend(exits)

            if ce_buys and pe_buys:
                # Rule: Concurrent BUY signals for CE and PE on the same underlying clash.
                # Approve the one with the highest confidence, reject the other.
                max_ce = max(ce_buys, key=lambda s: float(s.get("confidence") or 0))
                max_pe = max(pe_buys, key=lambda s: float(s.get("confidence") or 0))
                conf_ce = float(max_ce.get("confidence") or 0)
                conf_pe = float(max_pe.get("confidence") or 0)

                if conf_ce > conf_pe:
                    for sig in ce_buys:
                        if sig["id"] == max_ce["id"]:
                            approved.append(sig)
                        else:
                            sig["status"] = "FILTERED"
                            sig["rejection_reason"] = "duplicate-contract-buy"
                            rejected_or_filtered.append(sig)
                    for sig in pe_buys:
                        sig["status"] = "REJECTED"
                        sig["rejection_reason"] = "ce-pe-clash"
                        rejected_or_filtered.append(sig)
                elif conf_pe > conf_ce:
                    for sig in pe_buys:
                        if sig["id"] == max_pe["id"]:
                            approved.append(sig)
                        else:
                            sig["status"] = "FILTERED"
                            sig["rejection_reason"] = "duplicate-contract-buy"
                            rejected_or_filtered.append(sig)
                    for sig in ce_buys:
                        sig["status"] = "REJECTED"
                        sig["rejection_reason"] = "ce-pe-clash"
                        rejected_or_filtered.append(sig)
                else:
                    # Equal confidence - reject both
                    for sig in ce_buys:
                        sig["status"] = "REJECTED"
                        sig["rejection_reason"] = "ce-pe-clash"
                        rejected_or_filtered.append(sig)
                    for sig in pe_buys:
                        sig["status"] = "REJECTED"
                        sig["rejection_reason"] = "ce-pe-clash"
                        rejected_or_filtered.append(sig)
            else:
                # 3. Check duplicate buys for the exact same target contract
                contract_buys: Dict[str, List[Dict[str, Any]]] = {}
                for sig in (ce_buys + pe_buys):
                    target = str(sig.get("target_symbol") or "").upper()
                    contract_buys.setdefault(target, []).append(sig)

                for target, sig_list in contract_buys.items():
                    if len(sig_list) > 1:
                        winner = max(sig_list, key=lambda s: float(s.get("confidence") or 0))
                        for sig in sig_list:
                            if sig["id"] == winner["id"]:
                                approved.append(sig)
                            else:
                                sig["status"] = "FILTERED"
                                sig["rejection_reason"] = "duplicate-contract-buy"
                                rejected_or_filtered.append(sig)
                    else:
                        approved.extend(sig_list)

        return approved, rejected_or_filtered


class SignalManager:
    """Sweeps PENDING signals, evaluates cooldown/max daily limits, and coordinates execution."""

    @staticmethod
    async def validate_strategy_limits(db, strategy_id: str, user_id: str, visual_config: dict) -> Tuple[bool, Optional[str]]:
        risk = visual_config.get("risk") or {}
        
        # 1. Cooldown limits
        cooldown_min = int(risk.get("cooldown_minutes") or 0)
        strategy = await db.strategies.find_one({"id": strategy_id, "user_id": user_id})
        if not strategy:
            return False, "strategy-not-found"

        if cooldown_min > 0:
            last_sig_str = strategy.get("last_signal_at")
            if last_sig_str:
                try:
                    last_sig_time = datetime.fromisoformat(last_sig_str)
                    if last_sig_time.tzinfo is None:
                        last_sig_time = last_sig_time.replace(tzinfo=timezone.utc)
                    now_utc = datetime.now(timezone.utc)
                    elapsed = (now_utc - last_sig_time).total_seconds() / 60.0
                    if elapsed < cooldown_min:
                        return False, "cooldown-active"
                except Exception as e:
                    logger.warning(f"Failed parsing last_signal_at for {strategy_id}: {e}")

        # 2. Max trades limits
        max_trades = int(risk.get("max_trades_per_day") or 0)
        if max_trades > 0:
            now_utc = datetime.now(timezone.utc)
            local_midnight = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
            processed_count = await db.signals.count_documents({
                "strategy_id": strategy_id,
                "status": "PROCESSED",
                "created_at": {"$gte": local_midnight.isoformat()}
            })
            if processed_count >= max_trades:
                return False, "max-trades-day-reached"

        return True, None


async def _acquire_lock(db) -> bool:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=LOCK_TTL_SECONDS)
    try:
        row = await db.runner_locks.find_one_and_update(
            {
                "_id": LOCK_ID,
                "$or": [
                    {"expires_at": {"$lt": now}},
                    {"owner": POD_ID},
                ],
            },
            {"$set": {"owner": POD_ID, "expires_at": expires_at, "renewed_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if row:
            return True
        try:
            await db.runner_locks.insert_one({
                "_id": LOCK_ID, "owner": POD_ID,
                "expires_at": expires_at, "renewed_at": now,
            })
            return True
        except Exception:
            return False
    except Exception as e:
        logger.warning(f"Signal lock acquire failed: {e}")
        return False


async def _release_lock(db) -> None:
    try:
        await db.runner_locks.delete_one({"_id": LOCK_ID, "owner": POD_ID})
    except Exception:
        pass


async def signal_manager_loop(db, place_order_fn, stop_event: asyncio.Event) -> None:
    """Background signal sweeping loop."""
    logger.info(f"Signal Manager starting (tick={TICK_SECONDS}s, pod={POD_ID})")
    
    while not stop_event.is_set():
        owns_lock = await _acquire_lock(db)
        if not owns_lock:
            await asyncio.sleep(TICK_SECONDS)
            continue

        try:
            # 1. Fetch pending signals
            pending = await db.signals.find({"status": "PENDING"}).sort("created_at", 1).to_list(1000)
            if not pending:
                await asyncio.sleep(TICK_SECONDS)
                continue

            # Group signals by user_id
            by_user: Dict[str, List[Dict[str, Any]]] = {}
            for sig in pending:
                by_user.setdefault(sig["user_id"], []).append(sig)

            for user_id, sigs in by_user.items():
                try:
                    user_settings = await db.users.find_one({"id": user_id}, {"settings": 1})
                    settings = (user_settings or {}).get("settings") or {}
                    one_active_group = bool(settings.get("one_active_position_per_symbol_group", True))

                    # Retrieve all active strategy positions for user
                    active_positions = await db.strategy_positions.find({
                        "user_id": user_id,
                        "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]}
                    }).to_list(100)

                    # Pre-validate strategy-level limits (cooldown, trade count)
                    pre_validated = []
                    for sig in sigs:
                        visual_cfg = sig.get("visual_config") or {}
                        # Copy one_active_position_per_symbol_group option if present in visual_config
                        one_active_pos = visual_cfg.get("risk", {}).get("one_active_position_per_symbol_group", one_active_group)
                        
                        ok, limit_reason = await SignalManager.validate_strategy_limits(
                            db, sig["strategy_id"], user_id, visual_cfg
                        )
                        if not ok:
                            await db.signals.update_one(
                                {"id": sig["id"]},
                                {"$set": {
                                    "status": "FILTERED",
                                    "rejection_reason": limit_reason,
                                    "processed_at": datetime.now(timezone.utc).isoformat()
                                }}
                            )
                            sig["status"] = "FILTERED"
                            sig["rejection_reason"] = limit_reason
                        else:
                            pre_validated.append(sig)

                    # Coordinate conflicts across pre-validated signals
                    if pre_validated:
                        approved, rejected = ConflictResolver.resolve(
                            pre_validated, active_positions, one_active_position_per_symbol_group=one_active_group
                        )

                        # Handle rejected/filtered signals
                        for sig in rejected:
                            await db.signals.update_one(
                                {"id": sig["id"]},
                                {"$set": {
                                    "status": sig["status"],
                                    "rejection_reason": sig["rejection_reason"],
                                    "processed_at": datetime.now(timezone.utc).isoformat()
                                }}
                            )

                        # Dispatch approved signals
                        for sig in approved:
                            try:
                                opt_cfg = sig.get("visual_config", {}).get("options") or {}
                                option_contract = sig.get("option_contract")
                                
                                place_kwargs: Dict[str, Any] = dict(
                                    user_id=user_id,
                                    symbol=sig["symbol"],
                                    side=sig["action"],
                                    qty=int(opt_cfg.get("lots") or 1) if option_contract else None,
                                    order_type="MARKET",
                                    product=None,
                                    source=f"strategy:{sig['strategy_id']}",
                                    idempotency_key=f"sig:{sig['id']}",
                                    option_contract=option_contract,
                                )
                                
                                # Route direct strategy-level parameters
                                order_res = await place_order_fn(**place_kwargs)
                                
                                await db.signals.update_one(
                                    {"id": sig["id"]},
                                    {"$set": {
                                        "status": "PROCESSED",
                                        "order_id": order_res.get("id"),
                                        "processed_at": datetime.now(timezone.utc).isoformat()
                                    }}
                                )
                            except Exception as exec_err:
                                logger.warning(f"Failed order placement for signal {sig['id']}: {exec_err}")
                                await db.signals.update_one(
                                    {"id": sig["id"]},
                                    {"$set": {
                                        "status": "REJECTED",
                                        "rejection_reason": f"Execution failed: {str(exec_err)[:200]}",
                                        "processed_at": datetime.now(timezone.utc).isoformat()
                                    }}
                                )
                except Exception as user_err:
                    logger.warning(f"Error processing sweep batch for user {user_id}: {user_err}")
        except Exception as sweep_err:
            logger.warning(f"Signal sweep cycle encountered error: {sweep_err}")

        await asyncio.sleep(TICK_SECONDS)

    await _release_lock(db)
    logger.info("Signal Manager stopped")
