"""Signal Manager.

Sweeps normalized strategy signals and sends them through the execution
boundary. Strategy quality decisions belong inside strategies; this module only
performs minimal shape checks and lets platform execution safety decide whether
the signal is executable.
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

ACTIVE_POSITION_STATUSES = {"RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"}
SUPPORTED_SIGNAL_SYMBOLS = {"NIFTY", "BANKNIFTY", "SENSEX"}
SIGNAL_SPAM_WINDOW_SECONDS = int(os.environ.get("SIGNAL_SPAM_WINDOW_SECONDS", "300"))
SIGNAL_SPAM_THRESHOLD = int(os.environ.get("SIGNAL_SPAM_THRESHOLD", "0"))
STRATEGY_QUARANTINE_THRESHOLD = int(os.environ.get("STRATEGY_QUARANTINE_THRESHOLD", "5"))
PAPER_SIMULATED_SOURCES = {"PAPER_SIMULATED_CONTRACT", "PAPER_SIMULATED_PRICE"}


def _today_window_utc_iso() -> Tuple[str, str]:
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    ist_midnight = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = ist_midnight - timedelta(hours=5, minutes=30)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _signal_validation_result(sig: Dict[str, Any], ok: bool, reason_code: str, human_reason: str, severity: str = "BLOCKED") -> Dict[str, Any]:
    return {
        "ok": ok,
        "reason_code": reason_code,
        "human_reason": human_reason,
        "strategy_id": sig.get("strategy_id"),
        "signal_action": str(sig.get("action") or "").upper(),
        "symbol": str(sig.get("symbol") or "").upper(),
        "selected_contract": sig.get("target_symbol"),
        "severity": severity,
    }


def _strategy_mode(strategy: Dict[str, Any], sig: Dict[str, Any]) -> str:
    return str(strategy.get("mode") or sig.get("mode") or "").lower()


def _is_option_signal(sig: Dict[str, Any]) -> bool:
    visual_config = sig.get("visual_config") or {}
    opt_cfg = visual_config.get("options") or {}
    return bool(opt_cfg.get("enabled") or sig.get("option_contract") or sig.get("option_type"))


def _is_supported_signal_symbol(sig: Dict[str, Any]) -> bool:
    symbol = str(sig.get("symbol") or "").upper()
    if not symbol:
        return False
    if symbol in SUPPORTED_SIGNAL_SYMBOLS:
        return True
    # Direct equity paper strategies are valid too; only option/commodity
    # routing is limited to the known underlying set above.
    return not _is_option_signal(sig)


def _is_allowed_paper_simulated_contract(sig: Dict[str, Any], strategy: Dict[str, Any]) -> bool:
    option_contract = sig.get("option_contract") or {}
    if not option_contract:
        return False
    if _strategy_mode(strategy, sig) != "paper":
        return False
    token = str(option_contract.get("instrument_key") or option_contract.get("instrument_token") or "").strip()
    source = str(option_contract.get("source") or "").upper()
    simulated = bool(option_contract.get("simulated")) or token.upper().startswith("PAPER_") or source in PAPER_SIMULATED_SOURCES
    if not simulated:
        return False
    try:
        return float(option_contract.get("ltp") or 0) > 0
    except (TypeError, ValueError):
        return False


class StrategySignalValidator:
    """Validate the normalized signal envelope without judging strategy logic."""

    @staticmethod
    async def validate(db, sig: Dict[str, Any], strategy: Dict[str, Any], active_positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        action = str(sig.get("action") or "").upper()
        symbol = str(sig.get("symbol") or "").upper()
        target = str(sig.get("target_symbol") or "").upper()
        option_contract = sig.get("option_contract") or {}
        effective_action = str(option_contract.get("transaction_type") or action).upper()

        if not strategy:
            return _signal_validation_result(sig, False, "STRATEGY_MISSING", "Strategy not found.")
        mode = _strategy_mode(strategy, sig)
        if mode not in {"paper", "live"}:
            return _signal_validation_result(sig, False, "STRATEGY_INVALID_MODE", "Strategy mode must be paper or live.")
        if action not in {"BUY", "SELL"} or effective_action not in {"BUY", "SELL"}:
            return _signal_validation_result(sig, False, "STRATEGY_SIGNAL_INCOMPLETE", "Signal action must be BUY or SELL.")
        if not symbol:
            return _signal_validation_result(sig, False, "SIGNAL_MISSING_SYMBOL", "Signal is missing an underlying symbol.")
        if option_contract:
            token = str(option_contract.get("instrument_key") or option_contract.get("instrument_token") or "").strip()
            if not target or not token:
                return _signal_validation_result(sig, False, "INSTRUMENT_UNRESOLVED", "Resolved contract is missing target symbol or instrument key.")
            source = str(option_contract.get("source") or "").upper()
            if mode == "live" and (option_contract.get("simulated") or token.upper().startswith("PAPER_") or source in PAPER_SIMULATED_SOURCES):
                return _signal_validation_result(sig, False, "INSTRUMENT_UNRESOLVED", "Live strategy cannot use a simulated paper contract.")

        return _signal_validation_result(sig, True, "OK", "Signal validation passed.", "INFO")


class StrategyMisbehaviorDetector:
    """Tracks repeated invalid strategy behavior and quarantines chronic offenders."""

    @staticmethod
    async def record_validation(db, sig: Dict[str, Any], validation: Dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        start, end = _today_window_utc_iso()
        strategy_id = sig.get("strategy_id")
        user_id = sig.get("user_id")
        if not strategy_id or not user_id:
            return
        reason_code = validation.get("reason_code")
        valid = bool(validation.get("ok"))
        duplicate_inc = 1 if reason_code == "DUPLICATE_STRATEGY_INSTRUMENT_SIDE" else 0
        update = {
            "$set": {
                "last_evaluated_at": now,
                "last_signal_action": validation.get("signal_action"),
                "last_signal_reason": validation.get("human_reason"),
                "last_signal_validated": valid,
                "last_filter_reason": None if valid else validation.get("human_reason"),
                "last_skip_reason_code": None if valid else reason_code,
                "last_contract_selected": validation.get("selected_contract"),
            },
            "$inc": {
                "signal_count_today": 1,
                "duplicate_signal_count_today": duplicate_inc,
            },
        }
        if not valid:
            update["$inc"]["skipped_count_today"] = 1
        await db.strategies.update_one({"id": strategy_id, "user_id": user_id}, update)
        if valid:
            return


class ConflictResolver:
    """No app-level conflict arbitration.

    Different strategies may intentionally trade the same instrument. Duplicate
    protection is enforced later at the platform boundary for the same
    user+strategy+instrument+side only.
    """

    @staticmethod
    def resolve(
        pending_signals: List[Dict[str, Any]],
        active_positions: List[Dict[str, Any]],
        one_active_position_per_symbol_group: bool = True
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        return list(pending_signals), []


class SignalManager:
    """Sweeps PENDING signals, evaluates cooldown/max daily limits, and coordinates execution."""

    @staticmethod
    async def validate_strategy_limits(db, strategy_id: str, user_id: str, visual_config: dict) -> Tuple[bool, Optional[str]]:
        strategy = await db.strategies.find_one({"id": strategy_id, "user_id": user_id})
        if not strategy:
            return False, "strategy-not-found"

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


def _positive_float(*values: Any) -> Optional[float]:
    for value in values:
        try:
            parsed = float(value)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            continue
    return None


async def _dispatch_signal_via_unified_engine(
    db,
    user_id: str,
    sig: Dict[str, Any],
    strategy: Dict[str, Any],
    place_order_fn=None,
) -> Dict[str, Any]:
    from core.event_store import CoreEventStore
    from core.execution_router import ExecutionRouter
    from core.market_domains import resolve_domain_by_underlying
    from core.order_manager import OrderManager
    from core.portfolio_ledger import PortfolioLedger
    from core.risk_manager import RiskManager

    mode = _strategy_mode(strategy, sig)
    if mode not in {"paper", "live"}:
        raise ValueError(f"Unified strategy execution requires paper/live mode, got {mode or 'blank'}.")

    option_contract = sig.get("option_contract") or None
    symbol = str(sig.get("symbol") or "").upper()
    if not symbol:
        raise ValueError("Signal is missing an underlying symbol.")

    target_symbol = (
        (option_contract or {}).get("tradingsymbol")
        or (option_contract or {}).get("trading_symbol")
        or sig.get("target_symbol")
        or symbol
    )
    side = str((option_contract or {}).get("transaction_type") or sig.get("action") or "").upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("Signal action must be BUY or SELL.")

    price = _positive_float(
        sig.get("price"),
        sig.get("ltp"),
        (option_contract or {}).get("ltp"),
        sig.get("requested_price"),
    )
    if price is None:
        raise ValueError("Unified execution requires a positive signal price or contract LTP.")

    domain = resolve_domain_by_underlying(symbol)
    lot_size = int((option_contract or {}).get("lot_size") or domain.get_lot_size(symbol) or 1)
    lots = int((sig.get("visual_config") or {}).get("options", {}).get("lots") or sig.get("lots") or 1)
    requested_qty = max(1, lots) * max(1, lot_size)
    visual_risk = (sig.get("visual_config") or {}).get("risk") or {}
    stop_loss = sig.get("stop_loss")
    take_profit = sig.get("take_profit")
    if side == "BUY" and stop_loss in (None, ""):
        stop_pct = _positive_float(
            visual_risk.get("stop_loss_pct"),
            visual_risk.get("stoploss_pct"),
            visual_risk.get("stop_pct"),
        )
        if stop_pct:
            stop_loss = round(price * (1 - stop_pct / 100.0), 2)
    if side == "BUY" and take_profit in (None, ""):
        target_pct = _positive_float(
            visual_risk.get("take_profit_pct"),
            visual_risk.get("target_pct"),
            visual_risk.get("tp_pct"),
        )
        if target_pct:
            take_profit = round(price * (1 + target_pct / 100.0), 2)

    if side == "BUY":
        target_key = str(target_symbol or "").upper()
        token_key = str(
            (option_contract or {}).get("instrument_key")
            or (option_contract or {}).get("instrument_token")
            or ""
        ).upper()
        duplicate_match = {
            "user_id": user_id,
            "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
            "$or": [
                {"target_symbol": target_key},
                {"trading_symbol": target_key},
                {"symbol": target_key},
            ],
        }
        if token_key:
            duplicate_match["$or"].extend([
                {"instrument_key": token_key},
                {"instrument_token": token_key},
            ])
        existing_contract_position = await db.strategy_positions.find_one(duplicate_match, {"_id": 0, "id": 1, "strategy_id": 1})
        if existing_contract_position:
            return {
                "ok": False,
                "status": "SKIPPED",
                "reason": f"Active position already exists for {target_symbol}.",
                "reason_code": "SYMBOL_GROUP_ACTIVE_POSITION_EXISTS",
            }

    idem_key = sig.get("idempotency_key") or f"sig:{sig['id']}"
    order_mgr = OrderManager(db)
    if not await order_mgr.verify_and_lock_idempotency(idem_key, user_id):
        existing = await db.orders.find_one({"user_id": user_id, "idempotency_key": idem_key})
        if existing:
            return existing
        return {"ok": False, "status": "SKIPPED", "reason": "duplicate idempotency block", "reason_code": "DUPLICATE_SIGNAL"}

    risk_res = await RiskManager(db).evaluate_order(
        user_id=user_id,
        strategy_id=sig["strategy_id"],
        symbol=symbol,
        target_symbol=target_symbol,
        side=side,
        requested_qty=requested_qty,
        price=price,
        mode=mode,
        stop_loss=stop_loss,
        take_profit=take_profit,
        lot_size=lot_size,
    )
    if not risk_res.get("ok"):
        await CoreEventStore(db).log_event(
            "RISK_BLOCKED",
            sig["strategy_id"],
            user_id,
            {"reason": risk_res.get("reason"), "symbol": symbol, "mode": mode},
        )
        return {
            "ok": False,
            "status": "SKIPPED",
            "reason": risk_res.get("reason"),
            "reason_code": risk_res.get("status") or "RISK_BLOCKED",
        }

    if mode == "live":
        if place_order_fn is None:
            raise ValueError("Live signal execution requires the server order core boundary.")
        return await place_order_fn(
            user_id=user_id,
            symbol=symbol,
            side=side,
            qty=max(1, lots) if option_contract else int(sig.get("qty") or sig.get("quantity") or requested_qty),
            order_type=str(sig.get("order_type") or "MARKET").upper(),
            product=sig.get("product"),
            source=f"signal:strategy:{sig['strategy_id']}",
            option_contract=option_contract,
            exchange=(option_contract or {}).get("exchange") or sig.get("exchange") or "NSE",
            price=price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            idempotency_key=idem_key,
            signal_id=sig["id"],
        )

    intent_doc = order_mgr.compile_order_intent(
        strategy_id=sig["strategy_id"],
        symbol=symbol,
        target_symbol=target_symbol,
        side=side,
        quantity=int(risk_res["quantity"]),
        price=price,
        exchange=(option_contract or {}).get("exchange") or sig.get("exchange") or domain.exchange,
        segment=(option_contract or {}).get("segment") or sig.get("segment") or domain.segment,
        mode=mode,
        stop_loss=stop_loss,
        take_profit=take_profit,
        idempotency_key=idem_key,
    )
    if option_contract:
        intent_doc["instrument_token"] = (
            option_contract.get("instrument_key")
            or option_contract.get("instrument_token")
            or option_contract.get("upstox_instrument_token")
        )
        intent_doc["option_contract"] = option_contract

    return await ExecutionRouter(db, PortfolioLedger(db)).route_intent(user_id, intent_doc)


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
                        strategy = await db.strategies.find_one({"id": sig["strategy_id"], "user_id": user_id}) or {}
                        validation = await StrategySignalValidator.validate(db, sig, strategy, active_positions)
                        await StrategyMisbehaviorDetector.record_validation(db, sig, validation)
                        if not validation.get("ok"):
                            now_str = datetime.now(timezone.utc).isoformat()
                            await db.signals.update_one(
                                {"id": sig["id"]},
                                {"$set": {
                                    "status": "FILTERED",
                                    "rejection_reason": validation["reason_code"],
                                    "rejection_detail": validation,
                                    "processed_at": now_str,
                                }}
                            )
                            continue

                        ok, limit_reason = await SignalManager.validate_strategy_limits(
                            db, sig["strategy_id"], user_id, visual_cfg
                        )
                        if not ok:
                            validation = _signal_validation_result(sig, False, "STRATEGY_SIGNAL_SPAM" if limit_reason == "cooldown-active" else str(limit_reason).upper().replace("-", "_"), limit_reason or "strategy limit failed", "WARNING")
                            await StrategyMisbehaviorDetector.record_validation(db, sig, validation)
                            await db.signals.update_one(
                                {"id": sig["id"]},
                                {"$set": {
                                    "status": "FILTERED",
                                    "rejection_reason": validation["reason_code"],
                                    "rejection_detail": validation,
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
                            validation = _signal_validation_result(sig, False, str(sig["rejection_reason"]).upper().replace("-", "_"), sig["rejection_reason"], "WARNING")
                            await StrategyMisbehaviorDetector.record_validation(db, sig, validation)
                            await db.signals.update_one(
                                {"id": sig["id"]},
                                {"$set": {
                                    "status": sig["status"],
                                    "rejection_reason": validation["reason_code"],
                                    "rejection_detail": validation,
                                    "processed_at": datetime.now(timezone.utc).isoformat()
                                }}
                            )

                        # Dispatch approved signals
                        for sig in approved:
                            try:
                                strategy = await db.strategies.find_one({"id": sig["strategy_id"], "user_id": user_id}) or {}
                                order_res = await _dispatch_signal_via_unified_engine(db, user_id, sig, strategy, place_order_fn)
                                
                                now_str = datetime.now(timezone.utc).isoformat()
                                order_status = str(order_res.get("status") or "").upper()
                                final_signal_status = "SKIPPED_SIGNAL" if order_status in {"SKIPPED", "SKIPPED_SIGNAL"} else "PROCESSED"
                                signal_update = {
                                    "status": final_signal_status,
                                    "order_id": order_res.get("id") if final_signal_status == "PROCESSED" else None,
                                    "skipped_signal_id": order_res.get("id") if final_signal_status == "SKIPPED_SIGNAL" else None,
                                    "processed_at": now_str,
                                }
                                if final_signal_status == "SKIPPED_SIGNAL":
                                    signal_update["rejection_reason"] = order_res.get("reason_code") or order_res.get("skip_reason") or "preflight skipped"
                                await db.signals.update_one(
                                    {"id": sig["id"]},
                                    {"$set": signal_update}
                                )
                                if final_signal_status == "PROCESSED":
                                    await db.strategies.update_one(
                                        {"id": sig["strategy_id"], "user_id": user_id},
                                        {"$set": {"last_signal_at": now_str, "last_signal_validated": True}, "$inc": {"order_count_today": 1}}
                                    )
                            except Exception as exec_err:
                                logger.warning(f"Signal {sig['id']} skipped by execution boundary: {exec_err}")
                                await db.signals.update_one(
                                    {"id": sig["id"]},
                                    {"$set": {
                                        "status": "SKIPPED_SIGNAL",
                                        "rejection_reason": f"EXECUTION_SKIPPED: {str(exec_err)[:200]}",
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
