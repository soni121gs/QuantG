"""SQLite runtime state ledger for live option strategies.

This module is intentionally synchronous and small. FastAPI/Mongo remain the
application layer; SQLite owns the local ACID state machine that prevents
duplicate strategy entries during fast scanner loops.
"""
from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

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


@dataclass(frozen=True)
class LedgerDecision:
    accepted: bool
    reason: str
    state: Optional[str] = None
    active_position: Optional[Dict[str, Any]] = None


class SQLiteConnectionPool:
    def __init__(self, db_path: Path, size: int = 4):
        self.db_path = db_path
        self._pool: "queue.Queue[sqlite3.Connection]" = queue.Queue(maxsize=max(1, size))
        self._closed = False
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(max(1, size)):
            self._pool.put(self._new_connection())

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise RuntimeError("SQLite connection pool is closed")
        conn = self._pool.get()
        try:
            yield conn
        finally:
            self._pool.put(conn)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            while not self._pool.empty():
                conn = self._pool.get_nowait()
                conn.close()


class OptionStateLedger:
    def __init__(self, db_path: str | os.PathLike[str], pool_size: int = 4):
        self.pool = SQLiteConnectionPool(Path(db_path), size=pool_size)

    def initialize(self) -> None:
        with self.pool.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategy_states (
                    strategy_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL CHECK (state IN ('IDLE','OPEN','COOLDOWN','DISABLED')),
                    cooldown_until TEXT,
                    max_lots INTEGER NOT NULL DEFAULT 1,
                    target_pct REAL NOT NULL DEFAULT 0.40,
                    stoploss_pct REAL NOT NULL DEFAULT 0.20,
                    trailing_sl_enabled INTEGER NOT NULL DEFAULT 1,
                    trail_trigger_pct REAL NOT NULL DEFAULT 0.20,
                    trail_step_pct REAL NOT NULL DEFAULT 0.10,
                    cooldown_minutes INTEGER NOT NULL DEFAULT 5,
                    max_trades_day INTEGER NOT NULL DEFAULT 3,
                    required_capital REAL NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS open_positions (
                    strategy_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    option_type TEXT NOT NULL,
                    position_side TEXT NOT NULL DEFAULT 'LONG',
                    entry_price REAL NOT NULL,
                    ltp REAL NOT NULL,
                    target_price REAL NOT NULL,
                    stoploss_price REAL NOT NULL,
                    trailing_sl REAL,
                    highest_ltp REAL NOT NULL,
                    lowest_ltp REAL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    entry_time TEXT NOT NULL,
                    FOREIGN KEY(strategy_id) REFERENCES strategy_states(strategy_id)
                );

                CREATE TABLE IF NOT EXISTS trade_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    option_type TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    pnl REAL NOT NULL,
                    entry_time TEXT NOT NULL,
                    exit_time TEXT NOT NULL,
                    exit_reason TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS risk_settings (
                    strategy_id TEXT PRIMARY KEY,
                    daily_loss_limit REAL NOT NULL DEFAULT 0,
                    kill_switch_enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(strategy_id) REFERENCES strategy_states(strategy_id)
                );

                CREATE TABLE IF NOT EXISTS market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    ltp REAL NOT NULL,
                    data_source TEXT NOT NULL,
                    tick_time TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_pnl (
                    trade_date TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    realised_pnl REAL NOT NULL DEFAULT 0,
                    unrealised_pnl REAL NOT NULL DEFAULT 0,
                    trades INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (trade_date, strategy_id)
                );

                CREATE TABLE IF NOT EXISTS ai_trade_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    score REAL NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._ensure_column(conn, "open_positions", "position_side", "TEXT NOT NULL DEFAULT 'LONG'")
            self._ensure_column(conn, "open_positions", "lowest_ltp", "REAL")
        logger.info("SQLite option state ledger initialized")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.pool.close()

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
        cooldown_minutes: int = 20,
        max_trades_day: int = 2,
        required_capital: float = 0.0,
        daily_loss_limit: float = 0.0,
    ) -> None:
        max_lots = 1  # Core invariant: one lot maximum.
        with self.pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO strategy_states (
                    strategy_id, state, cooldown_until, max_lots, target_pct,
                    stoploss_pct, trailing_sl_enabled, trail_trigger_pct,
                    trail_step_pct, cooldown_minutes, max_trades_day, required_capital
                ) VALUES (?, 'IDLE', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    max_lots=1,
                    target_pct=excluded.target_pct,
                    stoploss_pct=excluded.stoploss_pct,
                    trailing_sl_enabled=excluded.trailing_sl_enabled,
                    trail_trigger_pct=excluded.trail_trigger_pct,
                    trail_step_pct=excluded.trail_step_pct,
                    cooldown_minutes=excluded.cooldown_minutes,
                    max_trades_day=excluded.max_trades_day,
                    required_capital=excluded.required_capital
                """,
                (
                    strategy_id,
                    max_lots,
                    target_pct,
                    stoploss_pct,
                    1 if trailing_sl_enabled else 0,
                    trail_trigger_pct,
                    trail_step_pct,
                    cooldown_minutes,
                    max_trades_day,
                    required_capital,
                ),
            )
            conn.execute(
                """
                INSERT INTO risk_settings (strategy_id, daily_loss_limit, kill_switch_enabled, updated_at)
                VALUES (?, ?, 0, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    daily_loss_limit=excluded.daily_loss_limit,
                    updated_at=excluded.updated_at
                """,
                (strategy_id, float(daily_loss_limit or 0), iso(utc_now())),
            )

    def set_kill_switch(self, enabled: bool, strategy_id: Optional[str] = None) -> int:
        with self.pool.connection() as conn:
            now = iso(utc_now())
            if strategy_id:
                conn.execute(
                    """
                    INSERT INTO risk_settings (strategy_id, kill_switch_enabled, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(strategy_id) DO UPDATE SET
                        kill_switch_enabled=excluded.kill_switch_enabled,
                        updated_at=excluded.updated_at
                    """,
                    (strategy_id, 1 if enabled else 0, now),
                )
                if enabled:
                    conn.execute("UPDATE strategy_states SET state='DISABLED' WHERE strategy_id=?", (strategy_id,))
                else:
                    conn.execute(
                        "UPDATE strategy_states SET state='IDLE', cooldown_until=NULL WHERE strategy_id=? AND state='DISABLED'",
                        (strategy_id,),
                    )
                return 1
            if enabled:
                conn.execute("UPDATE strategy_states SET state='DISABLED'")
            else:
                conn.execute("UPDATE strategy_states SET state='IDLE', cooldown_until=NULL WHERE state='DISABLED'")
            rows = conn.execute("SELECT strategy_id FROM strategy_states").fetchall()
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO risk_settings (strategy_id, kill_switch_enabled, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(strategy_id) DO UPDATE SET
                        kill_switch_enabled=excluded.kill_switch_enabled,
                        updated_at=excluded.updated_at
                    """,
                    (row["strategy_id"], 1 if enabled else 0, now),
                )
            return len(rows)

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
    ) -> tuple[float, float, Optional[float]]:
        entry = float(entry_price)
        side = _normalize_side(position_side)
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
        with self.pool.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = conn.execute(
                    "SELECT * FROM strategy_states WHERE strategy_id = ?",
                    (strategy_id,),
                ).fetchone()
                if state is None:
                    conn.execute(
                        "INSERT INTO strategy_states (strategy_id, state, max_lots) VALUES (?, 'IDLE', 1)",
                        (strategy_id,),
                    )
                    state = conn.execute(
                        "SELECT * FROM strategy_states WHERE strategy_id = ?",
                        (strategy_id,),
                    ).fetchone()

                current_state = state["state"]
                cooldown_until = parse_iso(state["cooldown_until"])
                if current_state == STATE_DISABLED:
                    conn.execute("ROLLBACK")
                    return LedgerDecision(False, "strategy-disabled", current_state)
                risk = conn.execute(
                    "SELECT * FROM risk_settings WHERE strategy_id = ?",
                    (strategy_id,),
                ).fetchone()
                if risk and risk["kill_switch_enabled"]:
                    conn.execute("ROLLBACK")
                    return LedgerDecision(False, "kill-switch-enabled", current_state)
                existing = conn.execute(
                    "SELECT * FROM open_positions WHERE strategy_id = ?",
                    (strategy_id,),
                ).fetchone()
                if existing:
                    if existing["status"] == POSITION_PENDING:
                        conn.execute("ROLLBACK")
                        return LedgerDecision(False, "pending-entry-exists", current_state, self._row_to_position(existing))
                    conn.execute("ROLLBACK")
                    return LedgerDecision(False, "duplicate-buy-dropped", current_state, self._row_to_position(existing))
                if current_state == STATE_OPEN:
                    conn.execute("ROLLBACK")
                    return LedgerDecision(False, "duplicate-buy-dropped", current_state)
                if current_state == STATE_COOLDOWN and cooldown_until and cooldown_until > now:
                    conn.execute("ROLLBACK")
                    return LedgerDecision(False, "cooldown-active", current_state)
                day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                trades_today = conn.execute(
                    "SELECT COUNT(*) AS n FROM trade_journal WHERE strategy_id=? AND exit_time >= ?",
                    (strategy_id, iso(day_start)),
                ).fetchone()["n"]
                if int(trades_today) >= int(state["max_trades_day"]):
                    conn.execute("ROLLBACK")
                    return LedgerDecision(False, "max-trades-day-reached", current_state)
                daily_pnl = conn.execute(
                    "SELECT realised_pnl FROM daily_pnl WHERE trade_date=? AND strategy_id=?",
                    (now.date().isoformat(), strategy_id),
                ).fetchone()
                daily_loss_limit = float(risk["daily_loss_limit"]) if risk else 0.0
                if daily_loss_limit > 0 and daily_pnl and float(daily_pnl["realised_pnl"]) <= -daily_loss_limit:
                    conn.execute("ROLLBACK")
                    return LedgerDecision(False, "daily-loss-limit-hit", current_state)

                target_price, stoploss_price, trailing_sl = self._brackets(
                    entry_price,
                    target_pct=float(state["target_pct"]),
                    stoploss_pct=float(state["stoploss_pct"]),
                    trailing_enabled=bool(state["trailing_sl_enabled"]),
                    trail_trigger_pct=float(state["trail_trigger_pct"]),
                    trail_step_pct=float(state["trail_step_pct"]),
                    position_side=side,
                )
                capped_quantity = min(quantity, max(1, int(state["max_lots"])))
                conn.execute(
                    """
                    INSERT OR REPLACE INTO open_positions (
                        strategy_id, symbol, option_type, position_side, entry_price, ltp,
                        target_price, stoploss_price, trailing_sl, highest_ltp, lowest_ltp,
                        quantity, status, entry_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        strategy_id,
                        symbol,
                        option_type,
                        side,
                        entry_price,
                        entry_price,
                        target_price,
                        stoploss_price,
                        trailing_sl,
                        entry_price,
                        entry_price,
                        capped_quantity,
                        POSITION_PENDING,
                        iso(now),
                    ),
                )
                conn.execute(
                    "UPDATE strategy_states SET state='OPEN', cooldown_until=NULL, max_lots=1 WHERE strategy_id=?",
                    (strategy_id,),
                )
                conn.execute("COMMIT")
                logger.info("option ledger pending strategy=%s symbol=%s side=%s qty=%s", strategy_id, symbol, side, capped_quantity)
                return LedgerDecision(True, "pending", STATE_OPEN, self.get_active_position(strategy_id))
            except Exception:
                conn.execute("ROLLBACK")
                raise

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
        with self.pool.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                pos = conn.execute(
                    "SELECT * FROM open_positions WHERE strategy_id = ?",
                    (strategy_id,),
                ).fetchone()
                state = conn.execute(
                    "SELECT * FROM strategy_states WHERE strategy_id = ?",
                    (strategy_id,),
                ).fetchone()
                if not pos:
                    if not create_if_missing:
                        conn.execute("ROLLBACK")
                        return LedgerDecision(False, "no-pending-position", state["state"] if state else None)
                    if state is None:
                        conn.execute(
                            "INSERT INTO strategy_states (strategy_id, state, max_lots) VALUES (?, 'IDLE', 1)",
                            (strategy_id,),
                        )
                        state = conn.execute(
                            "SELECT * FROM strategy_states WHERE strategy_id = ?",
                            (strategy_id,),
                        ).fetchone()
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
                    )
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO open_positions (
                            strategy_id, symbol, option_type, position_side, entry_price, ltp,
                            target_price, stoploss_price, trailing_sl, highest_ltp, lowest_ltp,
                            quantity, status, entry_time
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            strategy_id,
                            "",
                            "",
                            side,
                            fill_price,
                            fill_price,
                            target_price,
                            stoploss_price,
                            trailing_sl,
                            fill_price,
                            fill_price,
                            fill_qty,
                            POSITION_FILLED,
                            iso(now),
                        ),
                    )
                    conn.execute(
                        "UPDATE strategy_states SET state='OPEN', cooldown_until=NULL, max_lots=1 WHERE strategy_id=?",
                        (strategy_id,),
                    )
                    conn.execute("COMMIT")
                    return LedgerDecision(True, "filled", STATE_OPEN, self.get_active_position(strategy_id))
                if pos["status"] == POSITION_FILLED:
                    conn.execute("ROLLBACK")
                    return LedgerDecision(True, "already-filled", STATE_OPEN, self._row_to_position(pos))
                if pos["status"] == POSITION_CLOSED:
                    conn.execute("ROLLBACK")
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
                    )
                else:
                    target_price = float(pos["target_price"])
                    stoploss_price = float(pos["stoploss_price"])
                    trailing_sl = pos["trailing_sl"]

                conn.execute(
                    """
                    UPDATE open_positions SET
                        position_side=?,
                        entry_price=?,
                        ltp=?,
                        target_price=?,
                        stoploss_price=?,
                        trailing_sl=?,
                        highest_ltp=?,
                        lowest_ltp=?,
                        quantity=?,
                        status=?
                    WHERE strategy_id=?
                    """,
                    (
                        side,
                        fill_price,
                        fill_price,
                        target_price,
                        stoploss_price,
                        trailing_sl,
                        fill_price,
                        fill_price,
                        fill_qty,
                        POSITION_FILLED,
                        strategy_id,
                    ),
                )
                conn.execute(
                    "UPDATE strategy_states SET state='OPEN', cooldown_until=NULL, max_lots=1 WHERE strategy_id=?",
                    (strategy_id,),
                )
                conn.execute("COMMIT")
                logger.info("option ledger filled strategy=%s side=%s price=%s", strategy_id, side, fill_price)
                return LedgerDecision(True, "filled", STATE_OPEN, self.get_active_position(strategy_id))
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def close_position(self, strategy_id: str, exit_price: float, exit_reason: str) -> LedgerDecision:
        now = utc_now()
        with self.pool.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = conn.execute(
                    "SELECT * FROM strategy_states WHERE strategy_id = ?",
                    (strategy_id,),
                ).fetchone()
                pos = conn.execute(
                    "SELECT * FROM open_positions WHERE strategy_id = ?",
                    (strategy_id,),
                ).fetchone()
                if not state or not pos:
                    conn.execute("ROLLBACK")
                    return LedgerDecision(False, "no-open-position", state["state"] if state else None)
                if pos["status"] not in {POSITION_FILLED, POSITION_PENDING}:
                    conn.execute("ROLLBACK")
                    return LedgerDecision(False, "position-not-active", state["state"])

                side = _normalize_side(pos["position_side"])
                entry = float(pos["entry_price"])
                exit_val = float(exit_price)
                qty = int(pos["quantity"])
                if side == SIDE_SHORT:
                    pnl = round((entry - exit_val) * qty, 2)
                else:
                    pnl = round((exit_val - entry) * qty, 2)
                conn.execute(
                    "UPDATE open_positions SET status=?, ltp=? WHERE strategy_id=?",
                    (POSITION_CLOSED, exit_val, strategy_id),
                )
                conn.execute(
                    """
                    INSERT INTO trade_journal (
                        strategy_id, symbol, option_type, entry_price, exit_price,
                        quantity, pnl, entry_time, exit_time, exit_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        strategy_id,
                        pos["symbol"],
                        pos["option_type"],
                        pos["entry_price"],
                        exit_val,
                        qty,
                        pnl,
                        pos["entry_time"],
                        iso(now),
                        exit_reason,
                    ),
                )
                cooldown_until = now + timedelta(minutes=int(state["cooldown_minutes"]))
                conn.execute("DELETE FROM open_positions WHERE strategy_id = ?", (strategy_id,))
                conn.execute(
                    "UPDATE strategy_states SET state='COOLDOWN', cooldown_until=? WHERE strategy_id=?",
                    (iso(cooldown_until), strategy_id),
                )
                conn.execute(
                    """
                    INSERT INTO daily_pnl (trade_date, strategy_id, realised_pnl, unrealised_pnl, trades)
                    VALUES (?, ?, ?, 0, 1)
                    ON CONFLICT(trade_date, strategy_id) DO UPDATE SET
                        realised_pnl=realised_pnl + excluded.realised_pnl,
                        trades=trades + 1
                    """,
                    (now.date().isoformat(), strategy_id, pnl),
                )
                conn.execute("COMMIT")
                logger.info("option ledger closed strategy=%s pnl=%s reason=%s", strategy_id, pnl, exit_reason)
                return LedgerDecision(True, "closed", STATE_COOLDOWN)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def release_failed_open(self, strategy_id: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM open_positions WHERE strategy_id = ?", (strategy_id,))
                conn.execute(
                    "UPDATE strategy_states SET state='IDLE', cooldown_until=NULL WHERE strategy_id=? AND state='OPEN'",
                    (strategy_id,),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def advance_cooldowns(self) -> int:
        with self.pool.connection() as conn:
            res = conn.execute(
                """
                UPDATE strategy_states
                SET state='IDLE', cooldown_until=NULL
                WHERE state='COOLDOWN' AND cooldown_until IS NOT NULL AND cooldown_until <= ?
                """,
                (iso(utc_now()),),
            )
            return int(res.rowcount or 0)

    def update_ltp(self, strategy_id: str, ltp: float) -> Optional[Dict[str, Any]]:
        with self.pool.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                pos = conn.execute("SELECT * FROM open_positions WHERE strategy_id=?", (strategy_id,)).fetchone()
                if not pos or pos["status"] != POSITION_FILLED:
                    conn.execute("ROLLBACK")
                    return None
                state = conn.execute("SELECT * FROM strategy_states WHERE strategy_id=?", (strategy_id,)).fetchone()
                side = _normalize_side(pos["position_side"])
                highest_ltp = max(float(pos["highest_ltp"]), float(ltp))
                lowest_ltp = min(float(pos["lowest_ltp"] or pos["entry_price"]), float(ltp))
                trailing_sl = pos["trailing_sl"]
                if state and state["trailing_sl_enabled"]:
                    if side == SIDE_SHORT and lowest_ltp < float(pos["entry_price"]):
                        candidate = round(lowest_ltp * (1 + float(state["trail_step_pct"])), 2)
                        trailing_sl = min(float(trailing_sl or candidate), candidate)
                    elif side == SIDE_LONG and highest_ltp > float(pos["entry_price"]):
                        candidate = round(highest_ltp * (1 - float(state["trail_step_pct"])), 2)
                        trailing_sl = max(float(trailing_sl or 0), candidate)
                conn.execute(
                    "UPDATE open_positions SET ltp=?, highest_ltp=?, lowest_ltp=?, trailing_sl=? WHERE strategy_id=?",
                    (ltp, highest_ltp, lowest_ltp, trailing_sl, strategy_id),
                )
                entry = float(pos["entry_price"])
                qty = int(pos["quantity"])
                if side == SIDE_SHORT:
                    unrealised = round((entry - float(ltp)) * qty, 2)
                else:
                    unrealised = round((float(ltp) - entry) * qty, 2)
                conn.execute(
                    """
                    INSERT INTO daily_pnl (trade_date, strategy_id, realised_pnl, unrealised_pnl, trades)
                    VALUES (?, ?, 0, ?, 0)
                    ON CONFLICT(trade_date, strategy_id) DO UPDATE SET
                        unrealised_pnl=excluded.unrealised_pnl
                    """,
                    (utc_now().date().isoformat(), strategy_id, unrealised),
                )
                conn.execute("COMMIT")
                return self.get_active_position(strategy_id)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get_active_position(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        with self.pool.connection() as conn:
            return self._position_for_conn(conn, strategy_id)

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        self.advance_cooldowns()
        with self.pool.connection() as conn:
            states = conn.execute("SELECT * FROM strategy_states").fetchall()
            positions = {
                row["strategy_id"]: self._row_to_position(row)
                for row in conn.execute("SELECT * FROM open_positions").fetchall()
            }
            risk_rows = {
                row["strategy_id"]: row
                for row in conn.execute("SELECT * FROM risk_settings").fetchall()
            }
            pnl_rows = {
                row["strategy_id"]: row
                for row in conn.execute(
                    "SELECT * FROM daily_pnl WHERE trade_date=?",
                    (utc_now().date().isoformat(),),
                ).fetchall()
            }
        out = {}
        for row in states:
            sid = row["strategy_id"]
            risk_row = risk_rows.get(sid)
            pnl_row = pnl_rows.get(sid)
            out[sid] = {
                "strategy_id": row["strategy_id"],
                "state": row["state"],
                "cooldown_until": row["cooldown_until"],
                "max_lots": row["max_lots"],
                "target_pct": row["target_pct"],
                "stoploss_pct": row["stoploss_pct"],
                "trailing_sl_enabled": bool(row["trailing_sl_enabled"]),
                "trail_trigger_pct": row["trail_trigger_pct"],
                "trail_step_pct": row["trail_step_pct"],
                "cooldown_minutes": row["cooldown_minutes"],
                "max_trades_day": row["max_trades_day"],
                "required_capital": row["required_capital"],
                "active_position": positions.get(row["strategy_id"]),
                "risk_settings": {
                    "daily_loss_limit": risk_row["daily_loss_limit"] if risk_row else 0,
                    "kill_switch_enabled": bool(risk_row["kill_switch_enabled"]) if risk_row else False,
                },
                "daily_pnl": {
                    "realised_pnl": pnl_row["realised_pnl"] if pnl_row else 0,
                    "unrealised_pnl": pnl_row["unrealised_pnl"] if pnl_row else 0,
                    "trades": pnl_row["trades"] if pnl_row else 0,
                },
            }
        return out

    def open_positions(self) -> List[Dict[str, Any]]:
        with self.pool.connection() as conn:
            return [
                self._row_to_position(row)
                for row in conn.execute(
                    "SELECT * FROM open_positions WHERE status IN (?, ?)",
                    (POSITION_PENDING, POSITION_FILLED),
                ).fetchall()
            ]

    def record_market_tick(self, symbol: str, ltp: float, data_source: str, tick_time: Optional[datetime] = None) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO market_ticks (symbol, ltp, data_source, tick_time) VALUES (?, ?, ?, ?)",
                (symbol, float(ltp), data_source, iso(tick_time or utc_now())),
            )

    def latest_ticks(self, symbols: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
        with self.pool.connection() as conn:
            if symbols:
                placeholders = ",".join("?" for _ in symbols)
                rows = conn.execute(
                    f"""
                    SELECT mt.* FROM market_ticks mt
                    JOIN (
                        SELECT symbol, MAX(id) AS id FROM market_ticks
                        WHERE symbol IN ({placeholders})
                        GROUP BY symbol
                    ) latest ON latest.id = mt.id
                    """,
                    symbols,
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT mt.* FROM market_ticks mt
                    JOIN (
                        SELECT symbol, MAX(id) AS id FROM market_ticks GROUP BY symbol
                    ) latest ON latest.id = mt.id
                    """
                ).fetchall()
            return {
                row["symbol"]: {
                    "symbol": row["symbol"],
                    "ltp": row["ltp"],
                    "data_source": row["data_source"],
                    "tick_time": row["tick_time"],
                }
                for row in rows
            }

    def exit_signal_for_position(self, position: Dict[str, Any]) -> Optional[str]:
        if position.get("status") != POSITION_FILLED:
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

    def _position_for_conn(self, conn: sqlite3.Connection, strategy_id: str) -> Optional[Dict[str, Any]]:
        row = conn.execute("SELECT * FROM open_positions WHERE strategy_id=?", (strategy_id,)).fetchone()
        return self._row_to_position(row) if row else None

    def _row_to_position(self, row: sqlite3.Row) -> Dict[str, Any]:
        side = _normalize_side(row["position_side"] if "position_side" in row.keys() else SIDE_LONG)
        entry = float(row["entry_price"])
        ltp = float(row["ltp"])
        qty = int(row["quantity"])
        if side == SIDE_SHORT:
            unrealized_pnl = round((entry - ltp) * qty, 2)
        else:
            unrealized_pnl = round((ltp - entry) * qty, 2)
        return {
            "strategy_id": row["strategy_id"],
            "symbol": row["symbol"],
            "option_type": row["option_type"],
            "position_side": side,
            "entry_price": row["entry_price"],
            "ltp": row["ltp"],
            "target_price": row["target_price"],
            "stoploss_price": row["stoploss_price"],
            "trailing_sl": row["trailing_sl"],
            "highest_ltp": row["highest_ltp"],
            "lowest_ltp": row["lowest_ltp"] if "lowest_ltp" in row.keys() else row["entry_price"],
            "quantity": row["quantity"],
            "status": row["status"],
            "entry_time": row["entry_time"],
            "unrealized_pnl": unrealized_pnl,
        }
