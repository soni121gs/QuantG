"""Single source of truth for live execution state (positions, orders, SL/TP)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from execution_bridge import normalize_order_row, segment_from_exchange
from order_lifecycle import ORDER_ACTIVE_STATUSES, ORDER_TERMINAL_STATUSES, LEGACY_OPEN_STATUSES, LEGACY_TERMINAL_STATUSES

logger = logging.getLogger("quantg.execution_state")

OPEN_ORDER_STATUSES = set(ORDER_ACTIVE_STATUSES | LEGACY_OPEN_STATUSES)
TERMINAL_ORDER_STATUSES = set(ORDER_TERMINAL_STATUSES | LEGACY_TERMINAL_STATUSES | {"FAILED"})
DEFAULT_STOP_LOSS_PCT = 8.0   # Tightened from 22% — protects against large runaway losses
DEFAULT_TAKE_PROFIT_PCT = 20.0  # Reduced from 45% to maintain a sensible 2.5:1 R:R ratio


def _pct(row: Dict[str, Any], *keys: str, default: float) -> float:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            try:
                pct = float(value)
                return pct if pct > 1 else pct * 100.0
            except (TypeError, ValueError):
                continue
    return default


def _risk_prices(entry: float, side: str, risk: Dict[str, Any]) -> Dict[str, Optional[float]]:
    if entry <= 0:
        return {"stop_loss": None, "take_profit": None}
    stop_loss = risk.get("stoploss_price") or risk.get("stop_loss")
    take_profit = risk.get("target_price") or risk.get("take_profit")
    side_text = str(side or "LONG").upper()
    if stop_loss in (None, ""):
        stop_pct = _pct(risk, "stop_loss_pct", "stoploss_pct", default=DEFAULT_STOP_LOSS_PCT)
        stop_loss = entry * (1 - stop_pct / 100) if side_text != "SHORT" else entry * (1 + stop_pct / 100)
    if take_profit in (None, ""):
        target_pct = _pct(risk, "take_profit_pct", "target_pct", default=DEFAULT_TAKE_PROFIT_PCT)
        take_profit = entry * (1 + target_pct / 100) if side_text != "SHORT" else entry * (1 - target_pct / 100)
    return {
        "stop_loss": round(float(stop_loss), 2) if stop_loss not in (None, "") else None,
        "take_profit": round(float(take_profit), 2) if take_profit not in (None, "") else None,
    }


class ExecutionStateManager:
    """Fetches broker + DB state and returns one JSON payload for the UI."""

    def __init__(self) -> None:
        self._db: Any = None
        self._get_user_settings: Optional[Callable[[str], Awaitable[dict]]] = None
        self._get_user_kite: Optional[Callable[[str], Awaitable[Any]]] = None
        self._sync_kite_orders: Optional[Callable[[str, Any], Awaitable[dict]]] = None
        self._sync_kotak_orders: Optional[Callable[[str], Awaitable[dict]]] = None
        self._sync_upstox_orders: Optional[Callable[[str], Awaitable[dict]]] = None
        self._sync_strategy_positions: Optional[Callable[[str, Any], Awaitable[dict]]] = None
        self._fetch_positions: Optional[Any] = None
        self._option_ledger: Any = None
        self._last_sync_by_user: Dict[str, float] = {}

    def configure(
        self,
        *,
        db: Any,
        get_user_settings: Callable[[str], Awaitable[dict]],
        get_user_kite: Callable[[str], Awaitable[Any]],
        sync_kite_orders: Callable[[str, Any], Awaitable[dict]],
        sync_kotak_orders: Callable[[str], Awaitable[dict]],
        sync_upstox_orders: Callable[[str], Awaitable[dict]],
        sync_strategy_positions: Callable[[str, Any], Awaitable[dict]],
        fetch_positions: Any,
        option_ledger: Any,
    ) -> None:
        self._db = db
        self._get_user_settings = get_user_settings
        self._get_user_kite = get_user_kite
        self._sync_kite_orders = sync_kite_orders
        self._sync_kotak_orders = sync_kotak_orders
        self._sync_upstox_orders = sync_upstox_orders
        self._sync_strategy_positions = sync_strategy_positions
        self._fetch_positions = fetch_positions
        self._option_ledger = option_ledger

    async def sync_brokers(self, user_id: str, user: dict) -> Dict[str, Any]:
        sync_meta: Dict[str, Any] = {"upstox": {}, "positions": {}}
        sync_meta["upstox"] = await self._sync_upstox_orders(user_id)
        sync_meta["positions"] = await self._sync_strategy_positions(user_id, None)
        return sync_meta

    async def _load_orders(self, user_id: str) -> List[Dict[str, Any]]:
        rows = await self._db.orders.find(
            {"user_id": user_id, "visibility": {"$ne": "hidden"}},
            {"_id": 0, "user_id": 0},
        ).sort("created_at", -1).to_list(200)
        orders = [normalize_order_row(row) for row in rows]
        strategy_ids = {str(o.get("strategy_id")) for o in orders if o.get("strategy_id")}
        if strategy_ids:
            strategies = await self._db.strategies.find(
                {"user_id": user_id, "id": {"$in": list(strategy_ids)}},
                {"_id": 0, "id": 1, "name": 1},
            ).to_list(500)
            name_by_id = {str(s.get("id")): s.get("name") for s in strategies}
            for order in orders:
                sid = str(order.get("strategy_id") or "")
                if sid and name_by_id.get(sid):
                    order["strategy_name"] = name_by_id[sid]
        return orders

    async def _load_strategy_positions(self, user_id: str) -> List[Dict[str, Any]]:
        rows = await self._db.strategy_positions.find(
            {
                "user_id": user_id,
                "status": {
                    "$in": [
                        "RESERVED",
                        "PENDING_OPEN",
                        "PENDING_BROKER",
                        "OPEN",
                        "FILLED",
                        "EXITING",
                    ]
                },
            },
            {"_id": 0, "user_id": 0},
        ).to_list(500)
        strategy_ids = {str(row.get("strategy_id")) for row in rows if row.get("strategy_id")}
        name_by_id: Dict[str, Any] = {}
        if strategy_ids:
            strategies = await self._db.strategies.find(
                {"user_id": user_id, "id": {"$in": list(strategy_ids)}},
                {"_id": 0, "id": 1, "name": 1},
            ).to_list(500)
            name_by_id = {str(s.get("id")): s.get("name") for s in strategies}
        out: List[Dict[str, Any]] = []
        for row in rows:
            risk = row.get("tp_sl_tsl_config") or {}
            avg_price = float(row.get("average_buy_price") or 0)
            position_side = row.get("position_side") or "LONG"
            prices = _risk_prices(avg_price, position_side, risk)
            stop_loss = risk.get("stoploss_price") or risk.get("stop_loss") or prices.get("stop_loss")
            take_profit = risk.get("target_price") or risk.get("take_profit") or prices.get("take_profit")
            out.append({
                "id": row.get("id"),
                "strategy_id": row.get("strategy_id"),
                "strategy_name": name_by_id.get(str(row.get("strategy_id") or "")),
                "symbol": row.get("trading_symbol") or row.get("symbol"),
                "exchange": row.get("exchange"),
                "segment": segment_from_exchange(row.get("exchange") or "NSE", row.get("asset_class") or "DIRECT"),
                "instrument_token": row.get("instrument_token"),
                "qty": int(row.get("open_quantity") or row.get("quantity") or 0),
                "avg_price": avg_price,
                "status": row.get("status"),
                "execution_status": row.get("status"),
                "position_side": position_side,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "entry_order_id": row.get("entry_order_id"),
                "broker_order_id": row.get("entry_broker_order_id") or row.get("broker_order_id"),
                "mode": row.get("mode") or "paper",  # default to paper — never assume live
            })
        return out

    def _ledger_risk_by_strategy(self) -> Dict[str, Dict[str, Any]]:
        if not self._option_ledger:
            return {}
        snapshot = self._option_ledger.snapshot()
        out: Dict[str, Dict[str, Any]] = {}
        for sid, row in snapshot.items():
            active = row.get("active_position") or {}
            if not active:
                continue
            out[sid] = {
                "stop_loss": active.get("stoploss_price"),
                "take_profit": active.get("target_price"),
                "trailing_sl": active.get("trailing_sl"),
                "entry_price": active.get("entry_price"),
                "ltp": active.get("ltp"),
                "unrealized_pnl": active.get("unrealized_pnl"),
                "position_status": active.get("status"),
                "position_side": active.get("position_side"),
                "symbol": active.get("symbol"),
            }
        return out

    def _merge_position_risk(
        self,
        broker_positions: List[Dict[str, Any]],
        strategy_rows: List[Dict[str, Any]],
        ledger_risk: Dict[str, Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        by_symbol: Dict[str, Dict[str, Any]] = {}
        for row in broker_positions:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            merged = dict(row)
            merged["execution_status"] = merged.get("execution_status") or "FILLED"
            merged["stop_loss"] = row.get("stop_loss")
            merged["take_profit"] = row.get("take_profit")
            by_symbol[symbol] = merged

        for sp in strategy_rows:
            symbol = str(sp.get("symbol") or "").upper()
            if not symbol:
                continue
            ledger = ledger_risk.get(sp.get("strategy_id") or "", {})
            stop_loss = sp.get("stop_loss") or ledger.get("stop_loss")
            take_profit = sp.get("take_profit") or ledger.get("take_profit")
            trailing_sl = ledger.get("trailing_sl")
            base = by_symbol.get(symbol, {
                "symbol": symbol,
                "qty": sp.get("qty") or 0,
                "avg_price": sp.get("avg_price") or ledger.get("entry_price") or 0,
                "ltp": ledger.get("ltp") or sp.get("avg_price") or 0,
                "pnl": ledger.get("unrealized_pnl") or 0,
                "mode": sp.get("mode") or "live",
                "exchange": sp.get("exchange"),
                "segment": sp.get("segment"),
                "instrument_token": sp.get("instrument_token"),
                "broker": None,
            })
            base.update({
                "strategy_id": sp.get("strategy_id"),
                "strategy_name": sp.get("strategy_name"),
                "strategy_position_id": sp.get("id"),
                "execution_status": sp.get("execution_status"),
                "position_side": sp.get("position_side") or ledger.get("position_side") or "LONG",
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "trailing_sl": trailing_sl,
                "ledger_status": ledger.get("position_status"),
                "entry_order_id": sp.get("entry_order_id"),
                "broker_order_id": sp.get("broker_order_id"),
            })
            if ledger.get("ltp") is not None:
                base["ltp"] = ledger.get("ltp")
            if ledger.get("unrealized_pnl") is not None:
                base["pnl"] = ledger.get("unrealized_pnl")
            by_symbol[symbol] = base

        return list(by_symbol.values())

    async def build_snapshot(self, user: dict, *, sync: bool = True) -> Dict[str, Any]:
        user_id = user["id"]
        settings = await self._get_user_settings(user_id)
        sync_meta: Dict[str, Any] = {}
        if sync:
            now = time.monotonic()
            last_sync = self._last_sync_by_user.get(user_id, 0.0)
            if now - last_sync >= 5.0:
                try:
                    sync_meta = await self.sync_brokers(user_id, user)
                    self._last_sync_by_user[user_id] = now
                except Exception as exc:
                    logger.warning("execution snapshot broker sync failed user=%s: %s", user_id, exc)
                    sync_meta = {"error": str(exc)}
            else:
                sync_meta = {"throttled": True, "reason": "Sync requests throttled to at most once per 5 seconds."}

        broker_positions = await self._fetch_positions(user, settings)
        strategy_rows = await self._load_strategy_positions(user_id)
        ledger_risk = self._ledger_risk_by_strategy()
        positions = self._merge_position_risk(broker_positions, strategy_rows, ledger_risk)
        orders = await self._load_orders(user_id)

        open_orders = [o for o in orders if o.get("status") in OPEN_ORDER_STATUSES]
        failed_orders = [o for o in orders if o.get("status") == "FAILED"]

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "paper_mode": bool(settings.get("paper_mode", True)),
            "execution_broker": "upstox",
            "sync": sync_meta,
            "positions": positions,
            "orders": orders,
            "open_orders": open_orders,
            "failed_orders": failed_orders,
            "strategy_positions": strategy_rows,
            "ledger_strategies": ledger_risk,
            "summary": {
                "open_positions": len([p for p in positions if int(p.get("qty") or 0) != 0]),
                "open_orders": len(open_orders),
                "failed_orders": len(failed_orders),
                "total_unrealized_pnl": round(sum(float(p.get("pnl") or 0) for p in positions), 2),
            },
        }


execution_state_manager = ExecutionStateManager()
