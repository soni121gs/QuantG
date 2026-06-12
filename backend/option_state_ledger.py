"""MongoDB runtime state ledger for live option strategies.

This module is synchronous to align with the strategy evaluation loops,
but writes to MongoDB instead of SQLite to ensure durability, distributed safety,
and container scalability.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from pymongo import MongoClient

logger = logging.getLogger("quantg.option_ledger")

STATE_IDLE = "IDLE"
STATE_OPEN = "OPEN"
STATE_COOLDOWN = "COOLDOWN"
STATE_DISABLED = "DISABLED"

POSITION_PENDING = "PENDING"
POSITION_FILLED = "FILLED"
POSITION_CLOSED = "CLOSED"

SIDE_LONG = "LONG"
SIDE_SHORT = "SHORT"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ist_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _normalize_side(value: Optional[str]) -> str:
    text = str(value or SIDE_LONG).strip().upper()
    return SIDE_SHORT if text in {SIDE_SHORT, "SHORT", "SELL"} else SIDE_LONG


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _adaptive_exit_pcts(
    entry: float,
    *,
    target_pct: float,
    stoploss_pct: float,
    trail_trigger_pct: float,
    trail_step_pct: float,
    risk_style: str,
    adaptive_enabled: bool,
    target_r_multiple: float,
) -> tuple[float, float, float, float]:
    """Return decimal percentages tuned for option premium and strategy style."""
    if not adaptive_enabled or entry <= 0:
        return target_pct, stoploss_pct, trail_trigger_pct, trail_step_pct

    style = str(risk_style or "balanced").lower()
    style_bounds = {
        "micro_scalp": (0.035, 0.075, 1.15, 1.35),
        "momentum": (0.045, 0.095, 1.25, 1.55),
        "breakout": (0.055, 0.115, 1.35, 1.70),
        "volatile_breakout": (0.065, 0.135, 1.40, 1.85),
        "pullback": (0.045, 0.090, 1.25, 1.55),
        "balanced": (0.045, 0.100, 1.25, 1.55),
    }
    min_stop, max_stop, min_r, max_r = style_bounds.get(style, style_bounds["balanced"])

    premium_factor = 1.0
    if entry < 75:
        premium_factor = 1.18
    elif entry > 250:
        premium_factor = 0.88

    stop = _clamp(stoploss_pct * premium_factor, min_stop, max_stop)
    r_multiple = _clamp(float(target_r_multiple or min_r), min_r, max_r)
    target = _clamp(max(target_pct, stop * r_multiple), stop * min_r, stop * max_r)
    trigger = _clamp(min(trail_trigger_pct, stop * 0.75), 0.025, max(0.03, stop * 0.95))
    step = _clamp(min(trail_step_pct, stop * 0.45), 0.015, max(0.02, stop * 0.65))
    return target, stop, trigger, step


@dataclass(frozen=True)
class LedgerDecision:
    accepted: bool
    reason: str
    state: Optional[str] = None
    active_position: Optional[Dict[str, Any]] = None


class OptionStateLedger:
    def __init__(self, db_path: str | os.PathLike[str], pool_size: int = 4):
        self.db_path = db_path
        # Synchronous MongoDB Client using environment credentials
        mongo_uri = os.environ.get("MONGO_URI") or os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
        default_db = os.environ.get("DB_NAME", "quantg")
        
        path_str = str(db_path)
        # Use an explicit env var for test isolation — NEVER infer from path strings.
        # The old path-heuristic ("test" in path) could silently wipe production data
        # if the install directory contained the word "test".
        if os.environ.get("QUANTG_TEST_DB", "").lower() in ("1", "true", "yes"):
            self.db_name = f"{default_db}_test"
        else:
            self.db_name = default_db

        try:
            self.client = MongoClient(mongo_uri, serverSelectionTimeoutMS=1500)
            self.client.admin.command('ping')
        except Exception:
            if "localhost" not in mongo_uri and "127.0.0.1" not in mongo_uri:
                fallback_uri = "mongodb://localhost:27017"
                logger.warning("MongoDB host %s is not reachable; falling back to localhost", mongo_uri)
                self.client = MongoClient(fallback_uri)
            else:
                self.client = MongoClient(mongo_uri)

        self.db = self.client[self.db_name]
        self._lock = threading.Lock()
        logger.info("OptionStateLedger initialized using MongoDB client targeting: %s", self.db_name)

    def initialize(self) -> None:
        with self._lock:
            # Enforce high-performance database indexes
            self.db.option_strategy_states.create_index("strategy_id", unique=True)
            self.db.option_open_positions.create_index("strategy_id", unique=True)
            self.db.option_risk_settings.create_index("strategy_id", unique=True)
            self.db.option_daily_pnl.create_index([("trade_date", 1), ("strategy_id", 1)], unique=True)
            self.db.option_market_ticks.create_index([("symbol", 1), ("_id", -1)])
            self.db.option_trade_journal.create_index([("strategy_id", 1), ("exit_time", -1)])
            logger.info("MongoDB option state ledger collections & indexes verified successfully")

    def close(self) -> None:
        with self._lock:
            self.client.close()

    def upsert_strategy_state(
        self,
        strategy_id: str,
        *,
        max_lots: int = 1,
        target_pct: float = 0.45,
        stoploss_pct: float = 0.22,
        trailing_sl_enabled: bool = True,
        trail_trigger_pct: float = 0.25,
        trail_step_pct: float = 0.10,
        risk_style: str = "balanced",
        adaptive_exits_enabled: bool = True,
        target_r_multiple: float = 1.45,
        cooldown_minutes: int = 20,
        max_trades_day: int = 2,
        required_capital: float = 0.0,
        daily_loss_limit: float = 0.0,
        exit_mode: str = "tp_sl_tsl_or_signal",
    ) -> None:
        max_lots = 1  # Core invariant: one lot maximum.
        now = utc_now()
        with self._lock:
            self.db.option_strategy_states.update_one(
                {"strategy_id": strategy_id},
                {
                    "$set": {
                        "max_lots": max_lots,
                        "target_pct": target_pct,
                        "stoploss_pct": stoploss_pct,
                        "trailing_sl_enabled": 1 if trailing_sl_enabled else 0,
                        "trail_trigger_pct": trail_trigger_pct,
                        "trail_step_pct": trail_step_pct,
                        "risk_style": risk_style,
                        "adaptive_exits_enabled": 1 if adaptive_exits_enabled else 0,
                        "target_r_multiple": float(target_r_multiple or 1.45),
                        "cooldown_minutes": cooldown_minutes,
                        "max_trades_day": max_trades_day,
                        "required_capital": required_capital,
                        "exit_mode": exit_mode,
                        "updated_at": iso(now),
                    },
                    "$setOnInsert": {
                        "state": STATE_IDLE,
                        "cooldown_until": None,
                    }
                },
                upsert=True
            )
            self.db.option_risk_settings.update_one(
                {"strategy_id": strategy_id},
                {
                    "$set": {
                        "daily_loss_limit": float(daily_loss_limit or 0),
                        "kill_switch_enabled": 0,
                        "updated_at": iso(now),
                    }
                },
                upsert=True
            )

    def set_kill_switch(self, enabled: bool, strategy_id: Optional[str] = None) -> int:
        now_str = iso(utc_now())
        val = 1 if enabled else 0
        with self._lock:
            if strategy_id:
                self.db.option_risk_settings.update_one(
                    {"strategy_id": strategy_id},
                    {"$set": {"kill_switch_enabled": val, "updated_at": now_str}},
                    upsert=True
                )
                if enabled:
                    self.db.option_strategy_states.update_one(
                        {"strategy_id": strategy_id},
                        {"$set": {"state": STATE_DISABLED}}
                    )
                else:
                    self.db.option_strategy_states.update_one(
                        {"strategy_id": strategy_id, "state": STATE_DISABLED},
                        {"$set": {"state": STATE_IDLE, "cooldown_until": None}}
                    )
                return 1

            self.db.option_risk_settings.update_many(
                {},
                {"$set": {"kill_switch_enabled": val, "updated_at": now_str}}
            )
            if enabled:
                self.db.option_strategy_states.update_many({}, {"$set": {"state": STATE_DISABLED}})
            else:
                self.db.option_strategy_states.update_many({"state": STATE_DISABLED}, {"$set": {"state": STATE_IDLE, "cooldown_until": None}})
            return self.db.option_strategy_states.count_documents({})

    @staticmethod
    def _brackets(
        entry_price: float,
        *,
        target_pct: float,
        stoploss_pct: float,
        trailing_enabled: bool,
        trail_trigger_pct: float,
        trail_step_pct: float,
        position_side: str,
        risk_style: str = "balanced",
        adaptive_enabled: bool = True,
        target_r_multiple: float = 1.45,
    ) -> tuple[float, float, Optional[float]]:
        entry = float(entry_price)
        side = _normalize_side(position_side)
        target_pct, stoploss_pct, trail_trigger_pct, trail_step_pct = _adaptive_exit_pcts(
            entry,
            target_pct=target_pct,
            stoploss_pct=stoploss_pct,
            trail_trigger_pct=trail_trigger_pct,
            trail_step_pct=trail_step_pct,
            risk_style=risk_style,
            adaptive_enabled=adaptive_enabled,
            target_r_multiple=target_r_multiple,
        )
        if side == SIDE_SHORT:
            target_price = round(entry * (1 - target_pct), 2)
            stoploss_price = round(entry * (1 + stoploss_pct), 2)
            trailing_sl = (
                round(entry * (1 - trail_trigger_pct + trail_step_pct), 2)
                if trailing_enabled
                else None
            )
            return target_price, stoploss_price, trailing_sl
        target_price = round(entry * (1 + target_pct), 2)
        stoploss_price = round(entry * (1 - stoploss_pct), 2)
        trailing_sl = (
            round(entry * (1 + trail_trigger_pct - trail_step_pct), 2)
            if trailing_enabled
            else None
        )
        return target_price, stoploss_price, trailing_sl

    def try_open_position(
        self,
        *,
        strategy_id: str,
        symbol: str,
        option_type: str,
        entry_price: float,
        quantity: int,
        position_side: str = SIDE_LONG,
        broker_confirmed: bool = False,
    ) -> LedgerDecision:
        if broker_confirmed:
            return self.confirm_position_fill(
                strategy_id=strategy_id,
                entry_price=entry_price,
                quantity=quantity,
                position_side=position_side,
                create_if_missing=True,
            )
        now = utc_now()
        quantity = max(1, int(quantity))
        side = _normalize_side(position_side)
        with self._lock:
            state = self.db.option_strategy_states.find_one({"strategy_id": strategy_id})
            if state is None:
                self.db.option_strategy_states.insert_one({
                    "strategy_id": strategy_id,
                    "state": STATE_IDLE,
                    "max_lots": 1,
                    "target_pct": 0.40,
                    "stoploss_pct": 0.20,
                    "trailing_sl_enabled": 1,
                    "trail_trigger_pct": 0.20,
                    "trail_step_pct": 0.10,
                    "risk_style": "balanced",
                    "adaptive_exits_enabled": 1,
                    "target_r_multiple": 1.45,
                    "cooldown_minutes": 5,
                    "max_trades_day": 3,
                    "required_capital": 0.0,
                })
                state = self.db.option_strategy_states.find_one({"strategy_id": strategy_id})

            current_state = state["state"]
            cooldown_until = parse_iso(state.get("cooldown_until"))
            if current_state == STATE_DISABLED:
                return LedgerDecision(False, "strategy-disabled", current_state)

            risk = self.db.option_risk_settings.find_one({"strategy_id": strategy_id})
            if risk and risk.get("kill_switch_enabled"):
                return LedgerDecision(False, "kill-switch-enabled", current_state)

            existing = self.db.option_open_positions.find_one({"strategy_id": strategy_id})
            if existing:
                if existing.get("status") == POSITION_PENDING:
                    return LedgerDecision(False, "pending-entry-exists", current_state, self._row_to_position(existing))
                return LedgerDecision(False, "duplicate-buy-dropped", current_state, self._row_to_position(existing))

            if current_state == STATE_OPEN:
                return LedgerDecision(False, "duplicate-buy-dropped", current_state)

            if current_state == STATE_COOLDOWN and cooldown_until and cooldown_until > now:
                return LedgerDecision(False, "cooldown-active", current_state)

            # Start of today in IST, converted to a UTC ISO timestamp for trade journal query
            ist_today = ist_now()
            ist_midnight = ist_today.replace(hour=0, minute=0, second=0, microsecond=0)
            ist_midnight_utc = ist_midnight - timedelta(hours=5, minutes=30)
            trades_today = self.db.option_trade_journal.count_documents({
                "strategy_id": strategy_id,
                "exit_time": {"$gte": iso(ist_midnight_utc)}
            })
            if int(trades_today) >= int(state.get("max_trades_day", 3)):
                return LedgerDecision(False, "max-trades-day-reached", current_state)

            daily_pnl = self.db.option_daily_pnl.find_one({
                "trade_date": ist_today.date().isoformat(),
                "strategy_id": strategy_id
            })
            daily_loss_limit = float(risk["daily_loss_limit"]) if risk else 0.0
            # Safety fallback: prefer canonical realized_pnl, fall back to the
            # legacy realised_pnl spelling on any un-migrated doc — the daily-loss
            # kill switch must never silently read 0 and stop enforcing the limit.
            _daily_realized = (daily_pnl or {}).get("realized_pnl")
            if _daily_realized is None:
                _daily_realized = (daily_pnl or {}).get("realised_pnl", 0.0)
            if daily_loss_limit > 0 and daily_pnl and float(_daily_realized or 0.0) <= -daily_loss_limit:
                return LedgerDecision(False, "daily-loss-limit-hit", current_state)

            target_price, stoploss_price, trailing_sl = self._brackets(
                entry_price,
                target_pct=float(state.get("target_pct", 0.40)),
                stoploss_pct=float(state.get("stoploss_pct", 0.20)),
                trailing_enabled=bool(state.get("trailing_sl_enabled", 1)),
                trail_trigger_pct=float(state.get("trail_trigger_pct", 0.20)),
                trail_step_pct=float(state.get("trail_step_pct", 0.10)),
                position_side=side,
                risk_style=str(state.get("risk_style") or "balanced"),
                adaptive_enabled=bool(state.get("adaptive_exits_enabled", 1)),
                target_r_multiple=float(state.get("target_r_multiple", 1.45)),
            )
            # Quantity is stored as actual contracts, not lots. Max-lot
            # enforcement happens before order construction.
            capped_quantity = max(1, int(quantity))

            self.db.option_open_positions.update_one(
                {"strategy_id": strategy_id},
                {
                    "$set": {
                        "symbol": symbol,
                        "option_type": option_type,
                        "position_side": side,
                        "entry_price": entry_price,
                        "ltp": entry_price,
                        "target_price": target_price,
                        "stoploss_price": stoploss_price,
                        "trailing_sl": trailing_sl,
                        "highest_ltp": entry_price,
                        "lowest_ltp": entry_price,
                        "quantity": capped_quantity,
                        "status": POSITION_PENDING,
                        "entry_time": iso(now),
                    }
                },
                upsert=True
            )

            self.db.option_strategy_states.update_one(
                {"strategy_id": strategy_id},
                {"$set": {"state": STATE_OPEN, "cooldown_until": None, "max_lots": 1}}
            )

            logger.info("option ledger pending strategy=%s symbol=%s side=%s qty=%s", strategy_id, symbol, side, capped_quantity)
            return LedgerDecision(True, "pending", STATE_OPEN, self._position_for_lock(strategy_id))

    def confirm_position_fill(
        self,
        *,
        strategy_id: str,
        entry_price: Optional[float] = None,
        quantity: Optional[int] = None,
        position_side: Optional[str] = None,
        create_if_missing: bool = False,
    ) -> LedgerDecision:
        now = utc_now()
        with self._lock:
            pos = self.db.option_open_positions.find_one({"strategy_id": strategy_id})
            state = self.db.option_strategy_states.find_one({"strategy_id": strategy_id})

            if not pos:
                if not create_if_missing:
                    return LedgerDecision(False, "no-pending-position", state["state"] if state else None)
                if state is None:
                    self.db.option_strategy_states.insert_one({
                        "strategy_id": strategy_id,
                        "state": STATE_IDLE,
                        "max_lots": 1,
                        "target_pct": 0.40,
                        "stoploss_pct": 0.20,
                        "trailing_sl_enabled": 1,
                        "trail_trigger_pct": 0.20,
                        "trail_step_pct": 0.10,
                        "risk_style": "balanced",
                        "adaptive_exits_enabled": 1,
                        "target_r_multiple": 1.45,
                        "cooldown_minutes": 5,
                        "max_trades_day": 3,
                        "required_capital": 0.0,
                    })
                    state = self.db.option_strategy_states.find_one({"strategy_id": strategy_id})

                side = _normalize_side(position_side or SIDE_LONG)
                fill_price = float(entry_price or 0)
                fill_qty = max(1, int(quantity or 1))
                target_price, stoploss_price, trailing_sl = self._brackets(
                    fill_price,
                    target_pct=float(state["target_pct"]),
                    stoploss_pct=float(state["stoploss_pct"]),
                    trailing_enabled=bool(state["trailing_sl_enabled"]),
                    trail_trigger_pct=float(state["trail_trigger_pct"]),
                    trail_step_pct=float(state["trail_step_pct"]),
                    position_side=side,
                    risk_style=str(state.get("risk_style") or "balanced"),
                    adaptive_enabled=bool(state.get("adaptive_exits_enabled", 1)),
                    target_r_multiple=float(state.get("target_r_multiple", 1.45)),
                )
                self.db.option_open_positions.update_one(
                    {"strategy_id": strategy_id},
                    {
                        "$set": {
                            "symbol": "",
                            "option_type": "",
                            "position_side": side,
                            "entry_price": fill_price,
                            "ltp": fill_price,
                            "target_price": target_price,
                            "stoploss_price": stoploss_price,
                            "trailing_sl": trailing_sl,
                            "highest_ltp": fill_price,
                            "lowest_ltp": fill_price,
                            "quantity": fill_qty,
                            "status": POSITION_FILLED,
                            "entry_time": iso(now),
                        }
                    },
                    upsert=True
                )
                self.db.option_strategy_states.update_one(
                    {"strategy_id": strategy_id},
                    {"$set": {"state": STATE_OPEN, "cooldown_until": None, "max_lots": 1}}
                )
                return LedgerDecision(True, "filled", STATE_OPEN, self._position_for_lock(strategy_id))

            if pos["status"] == POSITION_FILLED:
                return LedgerDecision(True, "already-filled", STATE_OPEN, self._row_to_position(pos))
            if pos["status"] == POSITION_CLOSED:
                return LedgerDecision(False, "position-closed", state["state"] if state else None)

            side = _normalize_side(position_side or pos["position_side"])
            fill_price = float(entry_price if entry_price is not None else pos["entry_price"])
            fill_qty = max(1, int(quantity or pos["quantity"]))

            if state:
                target_price, stoploss_price, trailing_sl = self._brackets(
                    fill_price,
                    target_pct=float(state["target_pct"]),
                    stoploss_pct=float(state["stoploss_pct"]),
                    trailing_enabled=bool(state["trailing_sl_enabled"]),
                    trail_trigger_pct=float(state["trail_trigger_pct"]),
                    trail_step_pct=float(state["trail_step_pct"]),
                    position_side=side,
                    risk_style=str(state.get("risk_style") or "balanced"),
                    adaptive_enabled=bool(state.get("adaptive_exits_enabled", 1)),
                    target_r_multiple=float(state.get("target_r_multiple", 1.45)),
                )
            else:
                target_price = float(pos["target_price"])
                stoploss_price = float(pos["stoploss_price"])
                trailing_sl = pos["trailing_sl"]

            self.db.option_open_positions.update_one(
                {"strategy_id": strategy_id},
                {
                    "$set": {
                        "position_side": side,
                        "entry_price": fill_price,
                        "ltp": fill_price,
                        "target_price": target_price,
                        "stoploss_price": stoploss_price,
                        "trailing_sl": trailing_sl,
                        "highest_ltp": fill_price,
                        "lowest_ltp": fill_price,
                        "quantity": fill_qty,
                        "status": POSITION_FILLED,
                    }
                }
            )
            self.db.option_strategy_states.update_one(
                {"strategy_id": strategy_id},
                {"$set": {"state": STATE_OPEN, "cooldown_until": None, "max_lots": 1}}
            )
            logger.info("option ledger filled strategy=%s side=%s price=%s", strategy_id, side, fill_price)
            return LedgerDecision(True, "filled", STATE_OPEN, self._position_for_lock(strategy_id))

    def close_position(self, strategy_id: str, exit_price: float, exit_reason: str) -> LedgerDecision:
        now = utc_now()
        with self._lock:
            state = self.db.option_strategy_states.find_one({"strategy_id": strategy_id})
            pos = self.db.option_open_positions.find_one({"strategy_id": strategy_id})

            if not state or not pos:
                return LedgerDecision(False, "no-open-position", state["state"] if state else None)
            if pos["status"] not in {POSITION_FILLED, POSITION_PENDING}:
                return LedgerDecision(False, "position-not-active", state["state"])

            side = _normalize_side(pos["position_side"])
            entry = float(pos["entry_price"])
            exit_val = float(exit_price)
            qty = int(pos["quantity"])

            if side == SIDE_SHORT:
                pnl = round((entry - exit_val) * qty, 2)
            else:
                pnl = round((exit_val - entry) * qty, 2)

            self.db.option_open_positions.delete_one({"strategy_id": strategy_id})

            self.db.option_trade_journal.insert_one({
                "strategy_id": strategy_id,
                "symbol": pos.get("symbol", ""),
                "option_type": pos.get("option_type", ""),
                "entry_price": pos["entry_price"],
                "exit_price": exit_val,
                "quantity": qty,
                "pnl": pnl,
                "entry_time": pos["entry_time"],
                "exit_time": iso(now),
                "exit_reason": exit_reason,
            })

            cooldown_until = now + timedelta(minutes=int(state.get("cooldown_minutes", 5)))
            self.db.option_strategy_states.update_one(
                {"strategy_id": strategy_id},
                {"$set": {"state": STATE_COOLDOWN, "cooldown_until": iso(cooldown_until)}}
            )

            self.db.option_daily_pnl.update_one(
                {"trade_date": ist_now().date().isoformat(), "strategy_id": strategy_id},
                {
                    "$inc": {"realized_pnl": pnl, "trades": 1},
                    "$setOnInsert": {"unrealized_pnl": 0.0}
                },
                upsert=True
            )

            logger.info("option ledger closed strategy=%s pnl=%s reason=%s", strategy_id, pnl, exit_reason)
            return LedgerDecision(True, "closed", STATE_COOLDOWN)

    def release_failed_open(self, strategy_id: str) -> None:
        with self._lock:
            self.db.option_open_positions.delete_one({"strategy_id": strategy_id})
            self.db.option_strategy_states.update_one(
                {"strategy_id": strategy_id, "state": STATE_OPEN},
                {"$set": {"state": STATE_IDLE, "cooldown_until": None}}
            )

    def advance_cooldowns(self) -> int:
        now_str = iso(utc_now())
        with self._lock:
            res = self.db.option_strategy_states.update_many(
                {
                    "state": STATE_COOLDOWN,
                    "cooldown_until": {"$ne": None, "$lte": now_str}
                },
                {"$set": {"state": STATE_IDLE, "cooldown_until": None}}
            )
            return int(res.modified_count or 0)

    def update_ltp(self, strategy_id: str, ltp: float) -> Optional[Dict[str, Any]]:
        with self._lock:
            pos = self.db.option_open_positions.find_one({"strategy_id": strategy_id})
            if not pos or pos["status"] != POSITION_FILLED:
                return None

            state = self.db.option_strategy_states.find_one({"strategy_id": strategy_id})
            side = _normalize_side(pos["position_side"])
            highest_ltp = max(float(pos.get("highest_ltp", pos["entry_price"])), float(ltp))
            lowest_ltp = min(float(pos.get("lowest_ltp") or pos["entry_price"]), float(ltp))

            trailing_sl = pos.get("trailing_sl")
            if state and state.get("trailing_sl_enabled"):
                if side == SIDE_SHORT and lowest_ltp < float(pos["entry_price"]):
                    candidate = round(lowest_ltp * (1 + float(state["trail_step_pct"])), 2)
                    trailing_sl = min(float(trailing_sl or candidate), candidate)
                elif side == SIDE_LONG and highest_ltp > float(pos["entry_price"]):
                    candidate = round(highest_ltp * (1 - float(state["trail_step_pct"])), 2)
                    trailing_sl = max(float(trailing_sl or 0), candidate)

            self.db.option_open_positions.update_one(
                {"strategy_id": strategy_id},
                {
                    "$set": {
                        "ltp": ltp,
                        "highest_ltp": highest_ltp,
                        "lowest_ltp": lowest_ltp,
                        "trailing_sl": trailing_sl
                    }
                }
            )

            entry = float(pos["entry_price"])
            qty = int(pos["quantity"])
            if side == SIDE_SHORT:
                unrealized = round((entry - float(ltp)) * qty, 2)
            else:
                unrealized = round((float(ltp) - entry) * qty, 2)

            self.db.option_daily_pnl.update_one(
                {"trade_date": ist_now().date().isoformat(), "strategy_id": strategy_id},
                {
                    "$set": {"unrealized_pnl": unrealized},
                    "$setOnInsert": {"realized_pnl": 0.0, "trades": 0}
                },
                upsert=True
            )
            return self._position_for_lock(strategy_id)

    def get_active_position(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._position_for_lock(strategy_id)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        self.advance_cooldowns()
        with self._lock:
            states = list(self.db.option_strategy_states.find({}))
            positions_list = list(self.db.option_open_positions.find({}))
            positions = {pos["strategy_id"]: self._row_to_position(pos) for pos in positions_list}

            risk_rows = {risk["strategy_id"]: risk for risk in self.db.option_risk_settings.find({})}
            pnl_rows = {
                pnl["strategy_id"]: pnl
                for pnl in self.db.option_daily_pnl.find({"trade_date": ist_now().date().isoformat()})
            }

            out = {}
            for row in states:
                sid = row["strategy_id"]
                risk_row = risk_rows.get(sid)
                pnl_row = pnl_rows.get(sid)
                out[sid] = {
                    "strategy_id": row["strategy_id"],
                    "state": row["state"],
                    "cooldown_until": row.get("cooldown_until"),
                    "max_lots": row.get("max_lots", 1),
                    "target_pct": row.get("target_pct", 0.40),
                    "stoploss_pct": row.get("stoploss_pct", 0.20),
                    "trailing_sl_enabled": bool(row.get("trailing_sl_enabled", 1)),
                    "trail_trigger_pct": row.get("trail_trigger_pct", 0.20),
                    "trail_step_pct": row.get("trail_step_pct", 0.10),
                    "risk_style": row.get("risk_style", "balanced"),
                    "adaptive_exits_enabled": bool(row.get("adaptive_exits_enabled", 1)),
                    "target_r_multiple": row.get("target_r_multiple", 1.45),
                    "cooldown_minutes": row.get("cooldown_minutes", 5),
                    "max_trades_day": row.get("max_trades_day", 3),
                    "required_capital": row.get("required_capital", 0.0),
                    "active_position": positions.get(sid),
                    "risk_settings": {
                        "daily_loss_limit": risk_row["daily_loss_limit"] if risk_row else 0.0,
                        "kill_switch_enabled": bool(risk_row["kill_switch_enabled"]) if risk_row else False,
                    },
                    "daily_pnl": {
                        "realized_pnl": pnl_row["realized_pnl"] if pnl_row else 0.0,
                        "unrealized_pnl": pnl_row["unrealized_pnl"] if pnl_row else 0.0,
                        "trades": pnl_row["trades"] if pnl_row else 0,
                    },
                }
            return out

    def open_positions(self) -> List[Dict[str, Any]]:
        with self._lock:
            positions = self.db.option_open_positions.find({"status": {"$in": [POSITION_PENDING, POSITION_FILLED]}})
            return [self._row_to_position(pos) for pos in positions]

    def record_market_tick(self, symbol: str, ltp: float, data_source: str, tick_time: Optional[datetime] = None) -> None:
        with self._lock:
            self.db.option_market_ticks.insert_one({
                "symbol": symbol,
                "ltp": float(ltp),
                "data_source": data_source,
                "tick_time": iso(tick_time or utc_now()),
            })

    def latest_ticks(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            pipeline = []
            if symbols:
                pipeline.append({"$match": {"symbol": {"$in": symbols}}})
            
            pipeline.extend([
                {"$sort": {"_id": -1}},
                {
                    "$group": {
                        "_id": "$symbol",
                        "symbol": {"$first": "$symbol"},
                        "ltp": {"$first": "$ltp"},
                        "data_source": {"$first": "$data_source"},
                        "tick_time": {"$first": "$tick_time"}
                    }
                }
            ])
            
            results = list(self.db.option_market_ticks.aggregate(pipeline))
            return {
                row["symbol"]: {
                    "symbol": row["symbol"],
                    "ltp": row["ltp"],
                    "data_source": row["data_source"],
                    "tick_time": row["tick_time"],
                }
                for row in results
            }

    def exit_signal_for_position(self, position: Dict[str, Any]) -> Optional[str]:
        if position.get("status") != POSITION_FILLED:
            return None
        
        # Check strategy exit mode to determine if premium TP/SL checks should run
        strategy_id = position.get("strategy_id")
        state = self.db.option_strategy_states.find_one({"strategy_id": strategy_id})
        exit_mode = state.get("exit_mode", "tp_sl_tsl_or_signal") if state else "tp_sl_tsl_or_signal"
        if exit_mode in ("signal_only", "indicator_only"):
            return None

        ltp = float(position["ltp"])
        side = _normalize_side(position.get("position_side"))
        if side == SIDE_SHORT:
            if ltp <= float(position["target_price"]):
                return "target-hit"
            effective_sl = position.get("trailing_sl") or position.get("stoploss_price")
            if ltp >= float(effective_sl):
                return "trailing-sl-hit" if position.get("trailing_sl") else "stoploss-hit"
            return None
        if ltp >= float(position["target_price"]):
            return "target-hit"
        effective_sl = position.get("trailing_sl") or position.get("stoploss_price")
        if ltp <= float(effective_sl):
            return "trailing-sl-hit" if position.get("trailing_sl") else "stoploss-hit"
        return None

    def _position_for_lock(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        row = self.db.option_open_positions.find_one({"strategy_id": strategy_id})
        return self._row_to_position(row) if row else None

    def _row_to_position(self, row: Dict[str, Any]) -> Dict[str, Any]:
        side = _normalize_side(row.get("position_side") or SIDE_LONG)
        entry = float(row["entry_price"])
        ltp = float(row["ltp"])
        qty = int(row["quantity"])
        if side == SIDE_SHORT:
            unrealized_pnl = round((entry - ltp) * qty, 2)
        else:
            unrealized_pnl = round((ltp - entry) * qty, 2)
        return {
            "strategy_id": row["strategy_id"],
            "symbol": row.get("symbol", ""),
            "option_type": row.get("option_type", ""),
            "position_side": side,
            "entry_price": row["entry_price"],
            "ltp": row["ltp"],
            "target_price": row["target_price"],
            "stoploss_price": row["stoploss_price"],
            "trailing_sl": row.get("trailing_sl"),
            "highest_ltp": row.get("highest_ltp", entry),
            "lowest_ltp": row.get("lowest_ltp", entry),
            "quantity": row["quantity"],
            "status": row["status"],
            "entry_time": row["entry_time"],
            "unrealized_pnl": unrealized_pnl,
        }
