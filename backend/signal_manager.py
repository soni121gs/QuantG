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
from core.event_store import CoreEventStore
from pymongo import ReturnDocument
from trade_frequency import loss_streak_is_current

logger = logging.getLogger("quantg.signal_manager")

TICK_SECONDS = int(os.environ.get("SIGNAL_MANAGER_TICK_SECONDS", "2"))
LOCK_TTL_SECONDS = 90
LOCK_ID = "signal_manager"
POD_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"

ACTIVE_POSITION_STATUSES = {"RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"}
SUPPORTED_SIGNAL_SYMBOLS = {"NIFTY", "BANKNIFTY", "SENSEX"}
SIGNAL_SPAM_WINDOW_SECONDS = int(os.environ.get("SIGNAL_SPAM_WINDOW_SECONDS", "300"))
SIGNAL_SPAM_THRESHOLD = int(os.environ.get("SIGNAL_SPAM_THRESHOLD", "0"))
# 2026-07-09 (founder-directed): the loss-streak HARD BLOCK cut a strategy off after
# N consecutive SLs — but for a validated edge a losing streak is variance, not a
# broken strategy, and the block bites right before mean-reversion. Disabled by
# default (0). The soft size-throttle (below) still trims size on a streak without
# blocking. Set LOSS_STREAK_BLOCK_AT to e.g. 6 to re-arm the hard block.
LOSS_STREAK_BLOCK_AT = int(os.environ.get("LOSS_STREAK_BLOCK_AT", "0"))
# Defined-risk credit/debit spreads own their fill decision (both legs, capped loss);
# skip the buyer-oriented option-quality gate for them by default. "false" re-arms.
QUALITY_GATE_SKIP_SPREADS = os.environ.get("QUALITY_GATE_SKIP_SPREADS", "true").lower() == "true"
STRATEGY_QUARANTINE_THRESHOLD = int(os.environ.get("STRATEGY_QUARANTINE_THRESHOLD", "5"))
PAPER_SIMULATED_SOURCES = {"PAPER_SIMULATED_CONTRACT", "PAPER_SIMULATED_PRICE"}
PAPER_OPTION_MIN_SCORE = int(os.environ.get("PAPER_OPTION_MIN_SCORE", "35"))
LIVE_OPTION_MIN_SCORE = int(os.environ.get("LIVE_OPTION_MIN_SCORE", "70"))
PROFIT_CAP_BOOST_MAX = int(os.environ.get("PROFIT_CAP_BOOST_MAX", "2"))


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


# ---------------------------------------------------------------------------
# Priority scoring — pure function, no DB
# ---------------------------------------------------------------------------

def compute_priority_score(sig: Dict[str, Any]) -> float:
    """Compute the ranking score for an entry signal.

    Formula:
        priority_score =
            confidence                          (0-100)
          + option_quality_score * 0.4          (0-40)
          + target_R * 5                        (typically 0-25)
          - warnings_penalty                    (-5 per v2 warning, max -20)

    Safe defaults used when fields are missing.
    """
    confidence = float(sig.get("confidence") or 85.0)
    quality_raw = sig.get("option_quality_score")
    quality = float(quality_raw) if quality_raw is not None else 0.0
    target_r_raw = sig.get("target_R")
    target_r = float(target_r_raw) if target_r_raw is not None else 2.0
    warnings = sig.get("v2_selector_warnings") or []
    penalty = min(len(warnings) * 5.0, 20.0)
    score = confidence + quality * 0.4 + target_r * 5.0 - penalty
    return round(score, 4)


# ---------------------------------------------------------------------------
# Conflict resolver (synchronous — no DB)
# ---------------------------------------------------------------------------

class ConflictResolver:
    """Arbitrate signal conflicts using symbol-group locking and priority scoring."""

    @staticmethod
    def _get_symbol_group(item: Dict[str, Any]) -> str:
        # Check explicit symbol_group first
        g = item.get("symbol_group")
        if g:
            return str(g).upper().strip()
        
        # Fallback to symbol or target_symbol match
        # IMPORTANT: check BANKNIFTY before NIFTY because "NIFTY" is a substring of "BANKNIFTY"
        for field in ("symbol", "target_symbol"):
            val = item.get(field)
            if val:
                val_upper = str(val).upper().strip()
                for name in ("BANKNIFTY", "NIFTY", "SENSEX", "CRUDEOIL", "NATURALGAS"):
                    if name in val_upper:
                        return name
                return val_upper
        return "UNKNOWN"

    @staticmethod
    def resolve(
        pending_signals: List[Dict[str, Any]],
        active_positions: List[Dict[str, Any]],
        one_active_position_per_symbol_group: bool = True
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Returns (approved, rejected) lists.

        Each rejected signal carries:
          - rejection_reason: canonical reason_code string
          - priority_score: computed score
          - competing_signal_ids: list of competing signal ids
          - selected_signal_id: id of the winner (if applicable)

        Guarantees:
          1. Exits always win — never blocked by entry conflict logic.
          2. Active group exposure blocks new entries (SKIPPED_GROUP_EXPOSURE_ACTIVE).
          3. When multiple entries compete for the same group, the one with the
             highest priority_score wins (SKIPPED_LOWER_PRIORITY_SIGNAL for losers).
          4. Tie-breaker order: confidence → option_quality_score → created_at (newest) → strategy_id.
        """
        from collections import defaultdict

        # 1. Identify active positions that are not closed
        active_pos_by_strat: Dict[str, Dict[str, Any]] = {}
        for pos in active_positions:
            if pos.get("status") not in ("CLOSED", "EXITED"):
                active_pos_by_strat[pos["strategy_id"]] = pos

        # 2. Separate exits from entries
        exits: List[Dict[str, Any]] = []
        entries: List[Dict[str, Any]] = []
        exiting_strategies: set = set()

        for sig in pending_signals:
            strat_id = sig["strategy_id"]
            if strat_id in active_pos_by_strat:
                exits.append(sig)
                exiting_strategies.add(strat_id)
            else:
                entries.append(sig)

        # 3. Determine groups already locked by non-exiting active positions
        locked_groups: set = set()
        locked_group_position: Dict[str, str] = {}  # group -> strategy_id holding lock
        for strat_id, pos in active_pos_by_strat.items():
            if strat_id not in exiting_strategies:
                g = ConflictResolver._get_symbol_group(pos)
                locked_groups.add(g)
                locked_group_position[g] = strat_id

        approved: List[Dict[str, Any]] = list(exits)
        rejected: List[Dict[str, Any]] = []

        # 4. Compute priority scores for all entries
        for sig in entries:
            sig["_priority_score"] = compute_priority_score(sig)

        # 5. Group entries by symbol group
        group_entries: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        non_locking_entries: List[Dict[str, Any]] = []

        for sig in entries:
            g = ConflictResolver._get_symbol_group(sig)
            # Check per-signal override for locking
            sig_override = (sig.get("visual_config") or {}).get("risk", {}).get("one_active_position_per_symbol_group")
            use_lock = sig_override if sig_override is not None else one_active_position_per_symbol_group
            if use_lock:
                group_entries[g].append(sig)
            else:
                non_locking_entries.append(sig)

        # Non-locking signals always approved
        approved.extend(non_locking_entries)

        # 6. For each group: if locked → reject all; if contested → pick winner by priority
        for g, sigs in group_entries.items():
            if g in locked_groups:
                # Group already has an active position — block all new entries
                for s in sigs:
                    s["rejection_reason"] = "SKIPPED_GROUP_EXPOSURE_ACTIVE"
                    s["priority_score"] = s.pop("_priority_score", 0.0)
                    s["skip_reason"] = "SKIPPED_GROUP_EXPOSURE_ACTIVE"
                    s["competing_signal_ids"] = []
                    s["selected_signal_id"] = None
                rejected.extend(sigs)
                continue

            if len(sigs) == 1:
                # Only one entry for this group — approve directly
                s = sigs[0]
                s["priority_score"] = s.pop("_priority_score", 0.0)
                approved.append(s)
                locked_groups.add(g)
                continue

            # Multiple entries competing for same group — pick winner by priority_score
            def _sort_key(s: Dict[str, Any]) -> Tuple:
                return (
                    s.get("_priority_score", 0.0),               # higher is better
                    float(s.get("confidence") or 0.0),
                    float(s.get("option_quality_score") or 0.0),
                    # newest created_at wins on tie (lexicographic ISO string)
                    str(s.get("created_at") or ""),
                    # deterministic final tiebreak: larger strategy_id string wins
                    str(s.get("strategy_id") or ""),
                )

            ranked = sorted(sigs, key=_sort_key, reverse=True)
            winner = ranked[0]
            losers = ranked[1:]

            winner["priority_score"] = winner.pop("_priority_score", 0.0)
            competing_ids = [s["id"] for s in ranked]

            approved.append(winner)
            locked_groups.add(g)

            for s in losers:
                s["rejection_reason"] = "SKIPPED_LOWER_PRIORITY_SIGNAL"
                s["priority_score"] = s.pop("_priority_score", 0.0)
                s["skip_reason"] = "SKIPPED_LOWER_PRIORITY_SIGNAL"
                s["competing_signal_ids"] = competing_ids
                s["selected_signal_id"] = winner["id"]
                rejected.append(s)

        # 7. Restore original ordering and return
        approved_ids = {s["id"] for s in approved}
        final_approved = [s for s in pending_signals if s["id"] in approved_ids]
        final_rejected = [s for s in pending_signals if s["id"] not in approved_ids]

        # Sync rejection metadata for final_rejected list
        rejected_by_id = {s["id"]: s for s in rejected}
        for s in final_rejected:
            if s["id"] in rejected_by_id:
                r = rejected_by_id[s["id"]]
                s["rejection_reason"] = r.get("rejection_reason") or "conflict-blocked"
                s["priority_score"] = r.get("priority_score", 0.0)
                s["skip_reason"] = r.get("skip_reason") or s["rejection_reason"]
                s["competing_signal_ids"] = r.get("competing_signal_ids") or []
                s["selected_signal_id"] = r.get("selected_signal_id")
            else:
                s["rejection_reason"] = s.get("rejection_reason") or "conflict-blocked"
                s["priority_score"] = s.get("_priority_score", compute_priority_score(s))
                s.pop("_priority_score", None)

        logger.info(
            "ConflictResolver: %d approved, %d rejected. Locked groups: %s. Active positions: %d.",
            len(final_approved), len(final_rejected), sorted(locked_groups), len(active_positions)
        )
        return final_approved, final_rejected


async def store_priority_decisions(
    db,
    user_id: str,
    approved: List[Dict[str, Any]],
    rejected: List[Dict[str, Any]],
    strategy_map: Dict[str, Dict[str, Any]],
) -> None:
    """Persist priority decisions to db.signal_priority_decisions for diagnostics."""
    now = datetime.now(timezone.utc).isoformat()
    docs = []

    for sig in approved:
        strat = strategy_map.get(sig["strategy_id"]) or {}
        docs.append({
            "user_id": user_id,
            "strategy_id": sig.get("strategy_id"),
            "strategy_name": strat.get("name"),
            "symbol_group": ConflictResolver._get_symbol_group(sig),
            "action": sig.get("action"),
            "confidence": sig.get("confidence"),
            "option_quality_score": sig.get("option_quality_score"),
            "target_R": sig.get("target_R"),
            "priority_score": sig.get("priority_score"),
            "decision": "APPROVED",
            "reason_code": "APPROVED",
            "created_at": now,
        })

    for sig in rejected:
        strat = strategy_map.get(sig["strategy_id"]) or {}
        docs.append({
            "user_id": user_id,
            "strategy_id": sig.get("strategy_id"),
            "strategy_name": strat.get("name"),
            "symbol_group": ConflictResolver._get_symbol_group(sig),
            "action": sig.get("action"),
            "confidence": sig.get("confidence"),
            "option_quality_score": sig.get("option_quality_score"),
            "target_R": sig.get("target_R"),
            "priority_score": sig.get("priority_score", 0.0),
            "decision": "SKIPPED",
            "reason_code": sig.get("rejection_reason") or "CONFLICT_BLOCKED",
            "competing_signal_ids": sig.get("competing_signal_ids") or [],
            "selected_signal_id": sig.get("selected_signal_id"),
            "created_at": now,
        })

    if docs:
        try:
            await db.signal_priority_decisions.insert_many(docs, ordered=False)
        except Exception as e:
            logger.warning("Failed to store priority decisions: %s", e)


class SignalManager:
    """Sweeps PENDING signals, evaluates cooldown/max daily limits, and coordinates execution."""

    @staticmethod
    def _effective_max_trades(max_trades: int, strategy: Dict[str, Any], risk_cfg: Dict[str, Any]) -> int:
        today_pnl = _positive_float(strategy.get("today_pnl"))
        if today_pnl is None or today_pnl <= 0:
            return max_trades

        daily_loss_limit = _positive_float(risk_cfg.get("daily_loss_limit")) or 0.0
        boost = 1
        if daily_loss_limit > 0 and today_pnl >= daily_loss_limit:
            boost = min(PROFIT_CAP_BOOST_MAX, 2)
        else:
            boost = min(PROFIT_CAP_BOOST_MAX, boost)
        return max_trades + max(0, boost)

    @staticmethod
    async def validate_strategy_limits(db, strategy_id: str, user_id: str, visual_config: dict) -> Tuple[bool, Optional[str], float]:
        """Returns (ok, reason, allocation_multiplier).

        allocation_multiplier is 1.0 normally, 0.25 when loss-streak throttle fires (streak 3–4).
        ok=False when hard-blocked (streak >= 5, max trades, cooldown).
        """
        strategy = await db.strategies.find_one({"id": strategy_id, "user_id": user_id})
        if not strategy:
            return False, "strategy-not-found", 1.0

        # 0. Day profit lock — once a strategy (or the whole book) has booked its
        # gains for the day, position_monitor flags day_profit_locked and squares
        # off. Block any re-entry for the rest of that IST day. (See core/profit_lock.)
        # 0b. Day loss kill-switch — once a strategy (or the whole book) breaches its
        # daily loss floor, position_monitor flags day_loss_locked and squares off.
        # Block any re-entry for the rest of that IST day. (See core/loss_killswitch.)
        if strategy.get("day_loss_locked"):
            today_ist = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")
            if strategy.get("day_loss_locked_date") == today_ist:
                logger.info("[LIMITS] strategy=%s blocked: day-loss-locked (%s)",
                            strategy_id, strategy.get("day_loss_locked_reason"))
                return False, "loss-locked-day", 1.0

        risk_cfg = (
            (strategy.get("visual_config") or {}).get("risk")
            or (visual_config or {}).get("risk")
            or {}
        )
        is_paper_strategy = str(strategy.get("mode") or "").lower() == "paper"

        # 1. Max trades per day
        max_trades = risk_cfg.get("max_trades_day", risk_cfg.get("max_trades_per_day"))
        if max_trades:
            try:
                max_trades = int(max_trades)
            except (TypeError, ValueError):
                max_trades = None
        if is_paper_strategy:
            max_trades = max(int(max_trades or 0), int(os.environ.get("PAPER_MEASUREMENT_MAX_TRADES_DAY", "24")))
        if max_trades and max_trades > 0:
            effective_max_trades = SignalManager._effective_max_trades(max_trades, strategy, risk_cfg)
            count_today = int(strategy.get("order_count_today") or 0)
            if count_today >= effective_max_trades:
                logger.info(
                    "[LIMITS] strategy=%s blocked: max_trades_day=%d reached (today=%d, base=%d)",
                    strategy_id, effective_max_trades, count_today, max_trades,
                )
                return False, "max-trades-reached", 1.0

        # 2. Cooldown between trades
        cooldown_minutes = risk_cfg.get("cooldown_minutes")
        if cooldown_minutes:
            try:
                cooldown_minutes = int(cooldown_minutes)
            except (TypeError, ValueError):
                cooldown_minutes = None
        if is_paper_strategy and cooldown_minutes:
            cooldown_minutes = min(cooldown_minutes, int(os.environ.get("PAPER_MEASUREMENT_COOLDOWN_MINUTES", "3")))
        if cooldown_minutes and cooldown_minutes > 0:
            last_signal_at = strategy.get("last_signal_at")
            if last_signal_at:
                try:
                    if isinstance(last_signal_at, str):
                        last_dt = datetime.fromisoformat(last_signal_at.replace("Z", "+00:00"))
                    else:
                        last_dt = last_signal_at
                    elapsed_minutes = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60.0
                    if elapsed_minutes < cooldown_minutes:
                        remaining = round(cooldown_minutes - elapsed_minutes, 1)
                        logger.info(f"[LIMITS] strategy={strategy_id} blocked: cooldown active, {remaining}m remaining (cooldown={cooldown_minutes}m)")
                        return False, "cooldown-active", 1.0
                except Exception:
                    pass

        # 3. Loss-streak throttling
        streak_doc = await db.strategy_loss_streaks.find_one(
            {"strategy_id": strategy_id, "user_id": user_id},
            {"current_streak": 1, "last_sl_at": 1, "_id": 0},
        )
        streak = int((streak_doc or {}).get("current_streak") or 0)
        # Intraday breaker: a streak whose last SL was on a prior day does not carry
        # over to block today's first signals (fixes the all-signals-filtered 0-trade day).
        if streak and not loss_streak_is_current((streak_doc or {}).get("last_sl_at")):
            streak = 0
        if LOSS_STREAK_BLOCK_AT and streak >= LOSS_STREAK_BLOCK_AT:
            logger.info("[THROTTLE] strategy=%s BLOCKED: loss_streak=%d >= %d (blocked until next day reset)", strategy_id, streak, LOSS_STREAK_BLOCK_AT)
            return False, "loss-streak-blocked", 1.0
        if streak >= 5:
            logger.info("[THROTTLE] strategy=%s THROTTLED: loss_streak=%d >= 5 — allocation_multiplier=0.25", strategy_id, streak)
            return True, None, 0.25
        if streak >= 4:
            logger.info("[THROTTLE] strategy=%s THROTTLED: loss_streak=%d >= 4 — allocation_multiplier=0.50", strategy_id, streak)
            return True, None, 0.50

        return True, None, 1.0


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


async def _edge_math_spread_size(
    db, *, user_id: str, strategy: Dict[str, Any], spread: Dict[str, Any],
    lot_size: int, risk_budget: float, regime: str,
) -> tuple[int, Dict[str, Any]]:
    from core.edge_runtime import cached_rolling_edge_stats
    from core.edge_sizer import edge_size, payoff_ratio
    from core.spread_builder import lots_for_risk

    sid = strategy.get("id")
    vol_state = str(spread.get("vol_state") or "UNKNOWN")
    stats = await cached_rolling_edge_stats(
        db, user_id=user_id, strategy_id=sid, regime=regime,
        vol_state=vol_state,
    )
    wallet = await db.paper_wallets.find_one(
        {"user_id": user_id}, {"_id": 0, "balance": 1, "available_cash": 1},
    ) or {}
    equity = float(wallet.get("balance") or wallet.get("available_cash") or risk_budget)
    day_pnl = float(strategy.get("today_pnl") or 0)
    risk_cfg = (strategy.get("visual_config") or {}).get("risk") or {}
    daily_budget = float(risk_cfg.get("daily_loss_limit") or risk_budget or 1)
    peak_doc = await db.profit_lock_state.find_one(
        {"scope": "strat", "key": sid}, {"_id": 0, "peak": 1}, sort=[("updated_at", -1)],
    ) or {}
    per_lot_loss = float(spread.get("max_loss") or 0) * max(1, int(lot_size))
    decision = edge_size(
        stats=stats,
        payoff_b=payoff_ratio(stats.avg_win, stats.avg_loss),
        equity=equity,
        per_lot_max_loss=per_lot_loss,
        day_pnl=day_pnl,
        daily_risk_budget=daily_budget,
        peak_day_pnl=peak_doc.get("peak"),
        floor_lots=1 if str(strategy.get("mode") or "paper").lower() == "paper" else 0,
    )
    capital_cap = lots_for_risk(float(spread.get("max_loss") or 0), lot_size, risk_budget)
    contract_mult = max(0.10, min(1.0, float(spread.get("contract_size_mult") or 1.0)))
    profit_mult = max(0.10, min(1.0, float(strategy.get("day_profit_size_mult") or 1.0)))
    lots = min(max(1, capital_cap), max(1, int(round(decision.lots * contract_mult * profit_mult))))

    # RAE-4 router: gate/scale by regime OWNERSHIP. A credit spread is the RANGE
    # seller (RAE-3d); the router stands it down (size_mult 0) when the regime is
    # not RANGE/INSIDE — i.e. it refuses to sell premium into a TREND (the
    # 2026-07-10 loss) or a CHOP day. OBSERVE-ONLY by default: it always annotates
    # telemetry, but only changes lots when RAE_ROUTER_ENABLED=true (founder gate).
    from core.regime_router import route as _rae_route, enabled as _rae_enabled
    _router_on = _rae_enabled()
    # RAE-4: read the specialist ROLE the strategy declares (seed_regime_specialists
    # tags it under visual_config.options.specialist_role) instead of assuming every
    # credit spread is a range seller. An untagged spread is a legacy range-selling
    # credit spread (QG-O1/O4/O11) → default to 'range_seller' to preserve behavior.
    _opts = (strategy.get("visual_config") or {}).get("options") or {}
    _specialist = str(_opts.get("specialist_role") or "range_seller")
    # RAE-1 live: prefer the FINE intraday regime (HIGH_VOL_CHOP/INSIDE_QUIET + real
    # confidence, written to spread.router_regime by the caller) over the coarse
    # RANGE/TREND regime; fall back to coarse when the fine label isn't available yet.
    _routing = _rae_route(str(spread.get("router_regime") or regime or "UNKNOWN"),
                          float(spread.get("regime_confidence") or 0.5),
                          specialist=_specialist)
    if _router_on:
        lots = int(round(lots * _routing.size_mult))

    telemetry = {
        **decision.__dict__,
        "rolling_n": stats.n,
        "win_rate": stats.win_rate,
        "avg_win": stats.avg_win,
        "avg_loss": stats.avg_loss,
        "contract_mult": contract_mult,
        "profit_mult": profit_mult,
        "capital_cap_lots": capital_cap,
        "final_lots": lots,
        "regime": regime,
        "vol_state": vol_state,
        "router": {**_routing.as_dict(), "enforced": _router_on},
    }
    return lots, telemetry


async def _publish_signal_event(
    db,
    event_type: str,
    sig: Dict[str, Any],
    payload: Optional[Dict[str, Any]] = None,
    causation_id: Optional[str] = None,
) -> None:
    try:
        await CoreEventStore(db).log_signal_event(
            event_type,
            sig,
            payload=payload,
            causation_id=causation_id,
            source_module="signal_manager",
        )
    except Exception as exc:
        logger.warning(
            "Signal event publish failed type=%s sig=%s: %s",
            event_type,
            sig.get("id"),
            exc,
        )


async def _dispatch_signal_via_unified_engine(
    db,
    user_id: str,
    sig: Dict[str, Any],
    strategy: Dict[str, Any],
    place_order_fn=None,
) -> Dict[str, Any]:
    from core.execution_router import ExecutionRouter
    from core.market_domains import resolve_domain_by_underlying
    from core.order_manager import OrderManager
    from core.portfolio_ledger import PortfolioLedger
    from core.risk_manager import RiskManager

    mode = _strategy_mode(strategy, sig)
    if mode not in {"paper", "live"}:
        raise ValueError(f"Unified strategy execution requires paper/live mode, got {mode or 'blank'}.")

    # Hard gate: all live signals must pass the 12-point preflight before any order is created.
    # Paper signals are unaffected. A failed preflight returns SKIPPED_SIGNAL — no FAILED broker order.
    if mode == "live":
        from core.live_entry_preflight import live_entry_preflight
        pf = await live_entry_preflight(db, user_id, sig, strategy)
        if not pf["ok"]:
            logger.warning(
                "live_preflight BLOCKED sig=%s check=%s: %s",
                sig.get("id"), pf.get("failed_check"), pf.get("detail", ""),
            )
            return {
                "ok": False,
                "status": "SKIPPED_SIGNAL",
                "reason": f"LIVE_PREFLIGHT: {pf.get('failed_check')} — {pf.get('detail', '')}",
                "reason_code": "LIVE_PREFLIGHT_NOT_READY",
            }

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
        # Scope this dedup to the SAME strategy + mode, not the whole book. The
        # ledger nets fills by (strategy_id, target_symbol), so two DIFFERENT
        # strategies on the same contract land in disjoint buckets with their own
        # sizing/exits/attribution — there is no netting collision to protect
        # against. Without strategy_id this guard was cross-strategy: one strategy
        # holding a contract blocked every OTHER strategy's BUY on it, starving
        # them (idle). It should only stop ONE strategy from double-entering its
        # own contract. Correlation is capped separately by EXPOSURE_CAP.
        duplicate_match = {
            "user_id": user_id,
            "strategy_id": sig.get("strategy_id") or strategy.get("id"),
            "mode": mode,
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
                "reason": f"Active position already exists for {target_symbol} on this strategy.",
                "reason_code": "SYMBOL_GROUP_ACTIVE_POSITION_EXISTS",
            }

    # Symmetric guard for SELL: a strategy can emit an exit/SELL signal that
    # arrives AFTER the position was already closed by the monitor (TP/SL/time
    # exit) or a prior exit. With no live position to act on, the fill is a
    # redundant exit — the portfolio ledger rejects it as DUPLICATE_EXIT, which
    # surfaces as a REJECTED "NEEDS CHECK" order on the dashboard. We replicate
    # the ledger's exact DUPLICATE_EXIT condition here and SKIP cleanly BEFORE
    # creating an order: behaviour-preserving (the fill would have been rejected
    # anyway) but no doomed order, no wallet churn, no false alarm. A genuine
    # short entry (SELL with no prior CLOSED LONG on the symbol) is untouched.
    if side == "SELL":
        strat_id = sig.get("strategy_id") or strategy.get("id")
        live_pos = await db.strategy_positions.find_one(
            {
                "user_id": user_id,
                "strategy_id": strat_id,
                "target_symbol": target_symbol,
                "mode": mode,
                "status": {"$in": ["OPEN", "EXITING", "PENDING_BROKER"]},
            },
            {"_id": 0, "id": 1},
        )
        if not live_pos:
            closed_long = await db.strategy_positions.find_one(
                {
                    "user_id": user_id,
                    "strategy_id": strat_id,
                    "target_symbol": target_symbol,
                    "mode": mode,
                    "position_side": "LONG",
                    "status": "CLOSED",
                },
                {"_id": 0, "id": 1},
            )
            if closed_long:
                return {
                    "ok": False,
                    "status": "SKIPPED",
                    "reason": f"No live position to exit for {target_symbol}; already closed (redundant exit signal).",
                    "reason_code": "REDUNDANT_EXIT_NO_LIVE_POSITION",
                }

    idem_key = sig.get("idempotency_key") or f"sig:{sig['id']}"
    order_mgr = OrderManager(db)
    if not await order_mgr.verify_and_lock_idempotency(idem_key, user_id):
        existing = await db.orders.find_one({"user_id": user_id, "idempotency_key": idem_key})
        if existing:
            return existing
        return {"ok": False, "status": "SKIPPED", "reason": "duplicate idempotency block", "reason_code": "DUPLICATE_SIGNAL"}

    # Phase 2 #5: credit spread — open both legs as ONE position via the isolated
    # spread lifecycle, sized by defined risk (max loss). Bypasses the single-leg
    # risk/route path entirely. Opt-in per strategy + global CREDIT_SPREADS_ENABLED.
    _oc = option_contract or {}
    # HSI-13: stamp the market regime at entry onto the order so attribution is exact,
    # not reconstructed. Flows opt -> fill -> position_doc (single-leg) / passed through
    # to the spread lifecycle (spreads).
    _regime_at_entry = (
        (sig.get("regime_snapshot") or {}).get("regime")
        or sig.get("regime")
        or "UNKNOWN"
    )
    if option_contract is not None:
        option_contract["regime_at_entry"] = _regime_at_entry
    # RAE telemetry: also stamp the FINE regime (label + confidence) at entry from
    # the live regime engine, so the ensemble scorecard buckets by the RAE taxonomy
    # (INSIDE_QUIET / HIGH_VOL_CHOP / TREND_*) rather than only the coarse regime.
    # Best-effort + additive — this field NEVER gates or sizes anything; it is read
    # only by attribution/telemetry. Falls back to UNKNOWN on any miss.
    _regime_fine_at_entry = "UNKNOWN"
    _regime_fine_conf = None
    try:
        _fine_re = await db.market_regime_state.find_one(
            {"index": str(symbol).upper()},
            {"_id": 0, "regime_fine": 1, "regime_fine_confidence": 1},
        )
        if _fine_re and _fine_re.get("regime_fine"):
            _regime_fine_at_entry = str(_fine_re["regime_fine"]).upper()
            _regime_fine_conf = _fine_re.get("regime_fine_confidence")
    except Exception:
        pass
    if option_contract is not None:
        option_contract["regime_fine_at_entry"] = _regime_fine_at_entry
        option_contract["regime_fine_confidence_at_entry"] = _regime_fine_conf
    # Anti-pyramiding guard for spreads. A hold-to-theta / hold-to-expiry spread
    # strategy must hold ONE position per underlying, not open a fresh spread on
    # every runner cycle. The single-leg BUY dedup guard above keys on the exact
    # contract symbol, which shifts strike-by-strike for spreads (and SELL-entered
    # credit spreads bypass that guard entirely), so spreads slipped through and
    # pyramided — 8 stacked NIFTY/SENSEX spreads by mid-session, whose 2-leg REST
    # re-pricing then rate-limited (429) the quote feed and left every mark at ₹0.
    # Block a new spread entry when an active spread already exists for this
    # (user, strategy, underlying, mode). Exits are owned by the monitor/guardian
    # and are unaffected.
    if _oc.get("structure") in ("credit_spread", "debit_spread") and _oc.get("spread"):
        _existing_spread = await db.strategy_positions.find_one(
            {
                "user_id": user_id,
                "strategy_id": sig["strategy_id"],
                "underlying": symbol,
                "mode": mode,
                "structure": {"$in": ["credit_spread", "debit_spread"]},
                "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
            },
            {"_id": 0, "id": 1},
        )
        if _existing_spread:
            return {
                "ok": False,
                "status": "SKIPPED",
                "reason": f"Active spread already open for {symbol}; holding (no pyramiding).",
                "reason_code": "SPREAD_POSITION_ALREADY_OPEN",
            }

    if _oc.get("structure") == "credit_spread" and _oc.get("spread"):
        from core.spread_builder import CREDIT_SPREADS_ENABLED, lots_for_risk
        from core.spread_lifecycle import open_credit_spread
        if not CREDIT_SPREADS_ENABLED:
            return {"ok": False, "status": "SKIPPED", "reason": "credit spreads disabled",
                    "reason_code": "CREDIT_SPREADS_DISABLED"}
        _spread = _oc["spread"]
        _risk_budget = float(
            visual_risk.get("required_capital")
            or (sig.get("visual_config") or {}).get("options", {}).get("required_capital")
            or 15000.0
        )
        # RAE-1 live: pull the fine intraday regime (label + confidence) for this
        # underlying so the router gates on it. Best-effort; falls back to coarse.
        try:
            _fine = await db.market_regime_state.find_one(
                {"index": str(symbol).upper()},
                {"_id": 0, "regime_fine": 1, "regime_fine_confidence": 1},
            )
            if _fine and _fine.get("regime_fine"):
                _spread["router_regime"] = _fine["regime_fine"]
                _spread["regime_confidence"] = _fine.get("regime_fine_confidence") or 0.5
        except Exception:
            pass
        _spread_lots, _edge_telemetry = await _edge_math_spread_size(
            db, user_id=user_id, strategy=strategy, spread=_spread,
            lot_size=lot_size, risk_budget=_risk_budget, regime=_regime_at_entry,
        )
        _spread["edge_math"] = _edge_telemetry
        # RAE-4: when the router is ENFORCED (founder-gated) and stands this seller
        # down for the current regime (e.g. selling into a TREND/CHOP day), skip the
        # entry. Observe-only by default → this never fires until RAE_ROUTER_ENABLED.
        _rae = _edge_telemetry.get("router") or {}
        if _rae.get("enforced") and _rae.get("stand_down"):
            return {"ok": False, "status": "SKIPPED",
                    "reason": f"RAE router stand-down: {(_rae.get('reasons') or ['off-regime'])[0]}",
                    "reason_code": "RAE_ROUTER_STAND_DOWN"}
        # Per-strategy TP/SL geometry (visual_config.options.credit_tp_frac /
        # credit_sl_mult) — lets a credit scalp book 35% of credit with a 1.5x
        # stop while the theta book keeps the global env defaults.
        _sig_opts = (sig.get("visual_config") or {}).get("options", {}) or {}
        _strat_opts = (strategy.get("visual_config") or {}).get("options", {}) or {}
        # RES-2 entry gate (opt-in via visual_config.options.res2_gate): only sell
        # premium when IV−RV is rich AND the regime is sell-safe (RANGE). Validated
        # in RES-8 (turned the put spread NO_EDGE→CANDIDATE). FAIL-OPEN so a data
        # hiccup never silently stops the strategy. Exits are unaffected.
        if _sig_opts.get("res2_gate") or _strat_opts.get("res2_gate"):
            from core.entry_gate import evaluate_entry_gate_live
            _short_iv = ((_spread.get("short_leg") or {}).get("iv"))
            _min_edge = _sig_opts.get("res2_gate_min_edge", _strat_opts.get("res2_gate_min_edge"))
            _gate = await evaluate_entry_gate_live(
                underlying=symbol, regime=_regime_at_entry, iv=_short_iv,
                min_edge=float(_min_edge) if _min_edge is not None else 0.0,
            )
            if not _gate.get("allow"):
                return {"ok": False, "status": "SKIPPED",
                        "reason": _gate.get("reason") or "RES2_GATE blocked",
                        "reason_code": "RES2_GATE_BLOCKED"}
        _tp_frac = _sig_opts.get("credit_tp_frac", _strat_opts.get("credit_tp_frac"))
        _sl_mult = _sig_opts.get("credit_sl_mult", _strat_opts.get("credit_sl_mult"))
        return await open_credit_spread(
            db, user_id=user_id, strategy_id=sig["strategy_id"], underlying=symbol,
            spread=_spread, lots=_spread_lots, lot_size=lot_size, mode=mode,
            idempotency_key=idem_key, signal_id=sig["id"],
            regime_at_entry=_regime_at_entry,
            regime_fine_at_entry=_regime_fine_at_entry,
            regime_fine_confidence_at_entry=_regime_fine_conf,
            tp_frac=float(_tp_frac) if _tp_frac is not None else None,
            sl_mult=float(_sl_mult) if _sl_mult is not None else None,
        )

    if _oc.get("structure") == "debit_spread" and _oc.get("spread"):
        from core.spread_builder import DEBIT_SPREADS_ENABLED, lots_for_risk
        from core.spread_lifecycle import open_debit_spread
        if not DEBIT_SPREADS_ENABLED:
            return {"ok": False, "status": "SKIPPED", "reason": "debit spreads disabled",
                    "reason_code": "DEBIT_SPREADS_DISABLED"}
        _spread = _oc["spread"]
        _risk_budget = float(
            visual_risk.get("required_capital")
            or (sig.get("visual_config") or {}).get("options", {}).get("required_capital")
            or 15000.0
        )
        _spread_lots = max(1, lots_for_risk(_spread.get("max_loss") or 0, lot_size, _risk_budget))
        return await open_debit_spread(
            db, user_id=user_id, strategy_id=sig["strategy_id"], underlying=symbol,
            spread=_spread, lots=_spread_lots, lot_size=lot_size, mode=mode,
            idempotency_key=idem_key, signal_id=sig["id"],
            regime_at_entry=_regime_at_entry,
            regime_fine_at_entry=_regime_fine_at_entry,
            regime_fine_confidence_at_entry=_regime_fine_conf,
        )

    # RAE-3c: IV-cheap entry gate for directional delta-1 BUYERS (single leg).
    # Opt-in via visual_config.options.trend_iv_gate. Only fire the trend buyer when
    # the regime is a genuine TREND *and* options are cheap vs realized vol (the
    # mirror of the RES-2 seller gate) — the range-fakeout + vega filter that killed
    # every prior buyer. Regime is ALSO enforced by the RAE router; this is a second,
    # source-level line so the strategy's own entries stop firing off-regime. Sellers
    # /spreads already returned above, so this only touches single-leg buyers.

    # RAE-4 router for SINGLE-LEG specialists (trend delta-1). The spread path is
    # gated inside _edge_math_spread_size; single-leg buyers never went through it,
    # so the router had no effect on them and relied on the trend_iv_gate alone.
    # Gate here on the strategy's DECLARED specialist_role: stand the trend buyer
    # down on a regime it does not own (RANGE/CHOP), and scale size by the router
    # multiplier (trend precision → size). Only tagged specialists are affected;
    # untagged legacy single-leg buyers/equity have no specialist_role → skipped.
    # Observe-only until RAE_ROUTER_ENABLED=true (founder gate) — annotates only.
    from core.regime_router import route as _rae_route_sl, enabled as _rae_enabled_sl
    _sl_opts = (strategy.get("visual_config") or {}).get("options", {}) or {}
    _sl_specialist = str(_sl_opts.get("specialist_role") or "") or None
    if _sl_specialist:
        _sl_regime = _regime_fine_at_entry if _regime_fine_at_entry not in ("", "UNKNOWN") else _regime_at_entry
        _sl_routing = _rae_route_sl(str(_sl_regime or "UNKNOWN"),
                                    float(_regime_fine_conf or 0.5),
                                    specialist=_sl_specialist)
        _sl_router_on = _rae_enabled_sl()
        if option_contract is not None:
            option_contract["rae_router"] = {**_sl_routing.as_dict(), "enforced": _sl_router_on}
        if _sl_router_on and _sl_routing.stand_down:
            return {"ok": False, "status": "SKIPPED",
                    "reason": f"RAE router stand-down: {(_sl_routing.reasons or ['off-regime'])[0]}",
                    "reason_code": "RAE_ROUTER_STAND_DOWN"}
        if _sl_router_on and _sl_routing.size_mult != 1.0:
            lots = max(1, int(round(lots * _sl_routing.size_mult)))
            requested_qty = max(1, lots) * max(1, lot_size)

    _so = (sig.get("visual_config") or {}).get("options", {}) or {}
    _sto = (strategy.get("visual_config") or {}).get("options", {}) or {}
    if _so.get("trend_iv_gate") or _sto.get("trend_iv_gate"):
        from core.entry_gate import evaluate_buyer_gate_live
        _iv = _oc.get("iv") or (_oc.get("greeks") or {}).get("iv") or sig.get("iv")
        _min_cheap = _so.get("trend_iv_gate_min_cheap", _sto.get("trend_iv_gate_min_cheap"))
        _breg = _regime_fine_at_entry if _regime_fine_at_entry not in ("", "UNKNOWN") else _regime_at_entry
        _bgate = await evaluate_buyer_gate_live(
            underlying=symbol, regime=_breg, iv=_iv,
            min_cheap=float(_min_cheap) if _min_cheap is not None else 0.0,
        )
        if not _bgate.get("allow"):
            return {"ok": False, "status": "SKIPPED",
                    "reason": _bgate.get("reason") or "TREND_IV_GATE blocked",
                    "reason_code": "TREND_IV_GATE_BLOCKED"}

    risk_style = visual_risk.get("risk_style") or (strategy.get("visual_config") or {}).get("risk", {}).get("risk_style") or "balanced"
    product = (
        sig.get("product")
        or (sig.get("visual_config") or {}).get("options", {}).get("product")
        or (strategy.get("visual_config") or {}).get("options", {}).get("product")
    )
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
        risk_style=risk_style,
        product=product,
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

    if mode == "live" or (mode == "paper" and place_order_fn is not None):
        if mode == "live" and place_order_fn is None:
            raise ValueError("Live signal execution requires the server order core boundary.")
        final_qty = int(risk_res["quantity"])
        order_qty = final_qty
        if option_contract:
            order_qty = max(1, final_qty // max(1, lot_size))

        return await place_order_fn(
            user_id=user_id,
            symbol=symbol,
            side=side,
            qty=order_qty,
            order_type=str(sig.get("order_type") or "MARKET").upper(),
            product=product,
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
    # Carry lot sizing onto the intent so the fill path can round to whole lots
    # and the order doc records how many lots were actually placed (auditability).
    final_qty = int(risk_res["quantity"])
    intent_doc["lot_size"] = lot_size
    intent_doc["lots"] = max(1, final_qty // max(1, lot_size))
    intent_doc["product"] = product
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

            for sig in pending:
                await _publish_signal_event(
                    db,
                    "SIGNAL_QUEUED",
                    sig,
                    {"signal_status": "PENDING"},
                )

            # Group signals by user_id
            by_user: Dict[str, List[Dict[str, Any]]] = {}
            for sig in pending:
                by_user.setdefault(sig["user_id"], []).append(sig)

            for user_id, sigs in by_user.items():
                try:
                    user_settings = await db.users.find_one({"id": user_id}, {"settings": 1})
                    settings = (user_settings or {}).get("settings") or {}
                    # MULTI-STRATEGY: default False. The old default of True meant ONE
                    # position per underlying across the whole account — with 9 NIFTY/
                    # SENSEX strategies, the first fill locked everyone else out.
                    # Users can still opt in via settings for conservative accounts.
                    one_active_group = bool(settings.get("one_active_position_per_symbol_group", False))

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
                            await _publish_signal_event(
                                db,
                                "SIGNAL_VALIDATION_FAILED",
                                {**sig, "status": "FILTERED"},
                                {
                                    "signal_status": "FILTERED",
                                    "reason_code": validation["reason_code"],
                                    "human_reason": validation.get("human_reason"),
                                    "detail": validation,
                                },
                            )
                            continue

                        ok, limit_reason, alloc_mult = await SignalManager.validate_strategy_limits(
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
                            # Visibility: surface the limit-gate block (loss-streak /
                            # cooldown / max-trades) on the strategy doc so the UI shows
                            # WHY a strategy stopped trading, not just a silent zero.
                            await db.strategies.update_one(
                                {"id": sig["strategy_id"], "user_id": user_id},
                                {"$set": {
                                    "last_filter_reason": limit_reason,
                                    "last_skip_reason_code": validation["reason_code"],
                                    "last_filter_at": datetime.now(timezone.utc).isoformat(),
                                }},
                            )
                            sig["status"] = "FILTERED"
                            sig["rejection_reason"] = limit_reason
                            await _publish_signal_event(
                                db,
                                "SIGNAL_VALIDATION_FAILED",
                                sig,
                                {
                                    "signal_status": "FILTERED",
                                    "reason_code": validation["reason_code"],
                                    "human_reason": validation.get("human_reason"),
                                    "detail": validation,
                                },
                            )
                        else:
                            # Apply loss-streak allocation multiplier by scaling down lot count
                            if alloc_mult < 1.0:
                                vc = sig.get("visual_config") or {}
                                opts = vc.get("options") or {}
                                orig_lots = int(opts.get("lots") or 1)
                                throttled_lots = max(1, int(orig_lots * alloc_mult))
                                sig = {**sig, "visual_config": {**vc, "options": {**opts, "lots": throttled_lots}}}
                                sig["_throttle_alloc_mult"] = alloc_mult
                            # Option quality gate — enforce minimum score and block on
                            # quality_readiness: BLOCK before any order is dispatched.
                            # Thresholds match option_selector_v2: live >= 70, paper >= 50.
                            sig_mode = sig.get("mode") or "paper"
                            min_score = LIVE_OPTION_MIN_SCORE if sig_mode == "live" else PAPER_OPTION_MIN_SCORE
                            quality_score = sig.get("option_quality_score")
                            contract = sig.get("option_contract") or {}
                            trade_quality = contract.get("trade_quality_score") or {}
                            quality_readiness = contract.get("quality_readiness") or trade_quality.get("readiness", "")

                            # NO_DEPTH is informational: the score threshold still applies,
                            # but only a true BLOCK hard-stops the signal here.
                            quality_block_reason = None
                            _q_structure = str(contract.get("structure") or ((sig.get("visual_config") or {}).get("options") or {}).get("structure") or "")
                            if QUALITY_GATE_SKIP_SPREADS and _q_structure in ("credit_spread", "debit_spread"):
                                pass  # defined-risk spread: both legs priced, loss capped by wing — skip the buyer-oriented quality gate
                            elif quality_readiness == "BLOCK":
                                quality_block_reason = "option-quality-readiness-block"
                            elif quality_score is not None and float(quality_score) < min_score:
                                quality_block_reason = f"option-quality-score-low:{quality_score:.0f}<{min_score}"

                            if quality_block_reason:
                                logger.info(
                                    "[QUALITY] strategy=%s sig=%s filtered: %s",
                                    sig["strategy_id"], sig["id"], quality_block_reason,
                                )
                                await db.signals.update_one(
                                    {"id": sig["id"]},
                                    {"$set": {
                                        "status": "FILTERED",
                                        "rejection_reason": "OPTION_QUALITY_LOW",
                                        "rejection_detail": {"reason": quality_block_reason, "score": quality_score, "readiness": quality_readiness, "min_score": min_score, "score_components": (contract.get("trade_quality_score") or {}).get("components") or contract.get("quality_components")},
                                        "processed_at": datetime.now(timezone.utc).isoformat(),
                                    }}
                                )
                                sig["status"] = "FILTERED"
                                sig["rejection_reason"] = "OPTION_QUALITY_LOW"
                                await _publish_signal_event(
                                    db,
                                    "SIGNAL_VALIDATION_FAILED",
                                    sig,
                                    {
                                        "signal_status": "FILTERED",
                                        "reason_code": "OPTION_QUALITY_LOW",
                                        "human_reason": quality_block_reason,
                                        "detail": {
                                            "reason": quality_block_reason,
                                            "score": quality_score,
                                            "readiness": quality_readiness,
                                            "min_score": min_score,
                                        },
                                    },
                                )
                            else:
                                pre_validated.append(sig)

                    # Coordinate conflicts across pre-validated signals
                    if pre_validated:
                        # Build strategy name map for telemetry
                        strat_ids = {sig["strategy_id"] for sig in pre_validated}
                        strategy_map: Dict[str, Dict[str, Any]] = {}
                        for sid in strat_ids:
                            st = await db.strategies.find_one({"id": sid, "user_id": user_id}) or {}
                            strategy_map[sid] = st

                        approved, rejected = ConflictResolver.resolve(
                            pre_validated, active_positions, one_active_position_per_symbol_group=one_active_group
                        )

                        # Persist priority decisions for diagnostics
                        await store_priority_decisions(db, user_id, approved, rejected, strategy_map)

                        # Handle rejected/filtered signals
                        for sig in rejected:
                            validation = _signal_validation_result(sig, False, str(sig["rejection_reason"]).upper().replace("-", "_"), sig["rejection_reason"], "WARNING")
                            await StrategyMisbehaviorDetector.record_validation(db, sig, validation)
                            await db.signals.update_one(
                                {"id": sig["id"]},
                                {"$set": {
                                    "status": "SKIPPED_SIGNAL",
                                    "rejection_reason": sig["rejection_reason"],
                                    "rejection_detail": {
                                        "reason_code": sig["rejection_reason"],
                                        "priority_score": sig.get("priority_score", 0.0),
                                        "competing_signal_ids": sig.get("competing_signal_ids") or [],
                                        "selected_signal_id": sig.get("selected_signal_id"),
                                    },
                                    "processed_at": datetime.now(timezone.utc).isoformat()
                                }}
                            )
                            await _publish_signal_event(
                                db,
                                "SIGNAL_PRIORITY_SKIPPED",
                                {**sig, "status": "SKIPPED_SIGNAL"},
                                {
                                    "signal_status": "SKIPPED_SIGNAL",
                                    "reason_code": str(sig["rejection_reason"]).upper().replace("-", "_"),
                                    "human_reason": sig["rejection_reason"],
                                    "priority_score": sig.get("priority_score", 0.0),
                                    "competing_signal_ids": sig.get("competing_signal_ids") or [],
                                    "selected_signal_id": sig.get("selected_signal_id"),
                                },
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
                                    # Persist full rejection detail so EVERY dispatch-boundary skip
                                    # (sizing, greeks, daily-loss, preflight, duplicate, group
                                    # exposure) is diagnosable from the record — not just its code.
                                    # Prefer a structured detail from the rejecter; else fall back
                                    # to the reason/code/status the boundary returned.
                                    signal_update["rejection_detail"] = order_res.get("detail") or {
                                        "reason_code": order_res.get("reason_code"),
                                        "human_reason": order_res.get("reason") or order_res.get("skip_reason"),
                                        "status": order_res.get("status"),
                                    }
                                await db.signals.update_one(
                                    {"id": sig["id"]},
                                    {"$set": signal_update}
                                )
                                event_payload = {
                                    "signal_status": final_signal_status,
                                    "reason_code": signal_update.get("rejection_reason"),
                                    "order_id": signal_update.get("order_id"),
                                    "skipped_signal_id": signal_update.get("skipped_signal_id"),
                                    "detail": signal_update.get("rejection_detail"),
                                }
                                await _publish_signal_event(
                                    db,
                                    "SIGNAL_EXECUTION_SKIPPED" if final_signal_status == "SKIPPED_SIGNAL" else "SIGNAL_PROCESSED",
                                    {**sig, "status": final_signal_status},
                                    event_payload,
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
                                        "rejection_detail": {
                                            "reason_code": "EXECUTION_SKIPPED",
                                            "human_reason": str(exec_err)[:500],
                                        },
                                        "processed_at": datetime.now(timezone.utc).isoformat()
                                    }}
                                )
                                await _publish_signal_event(
                                    db,
                                    "SIGNAL_EXECUTION_SKIPPED",
                                    {**sig, "status": "SKIPPED_SIGNAL"},
                                    {
                                        "signal_status": "SKIPPED_SIGNAL",
                                        "reason_code": "EXECUTION_SKIPPED",
                                        "human_reason": str(exec_err)[:500],
                                    },
                                )
                except Exception as user_err:
                    logger.warning(f"Error processing sweep batch for user {user_id}: {user_err}")
        except Exception as sweep_err:
            logger.warning(f"Signal sweep cycle encountered error: {sweep_err}")

        await asyncio.sleep(TICK_SECONDS)

    await _release_lock(db)
    logger.info("Signal Manager stopped")
