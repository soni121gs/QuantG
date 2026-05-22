"""Single source of truth for live execution state (positions, orders, SL/TP)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from execution_bridge import normalize_order_row, segment_from_exchange

logger = logging.getLogger("quantg.execution_state")

OPEN_ORDER_STATUSES = {
    "OPEN",
    "PENDING",
    "PENDING_BROKER",
    "TRIGGER PENDING",
    "MODIFY PENDING",
    "VALIDATION PENDING",
    "PUT ORDER REQ RECEIVED",
}
TERMINAL_ORDER_STATUSES = {"COMPLETE", "CANCELLED", "REJECTED", "FAILED", "STALE", "BROKER_NOT_FOUND"}


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
        settings = await self._get_user_settings(user_id)
        kite, _ = await self._get_user_kite(user_id)
        sync_meta: Dict[str, Any] = {"kite": {}, "kotak": {}, "upstox": {}, "positions": {}}
        if kite and not settings.get("paper_mode", True):
            sync_meta["kite"] = await self._sync_kite_orders(user_id, kite)
        sync_meta["kotak"] = await self._sync_kotak_orders(user_id)
        sync_meta["upstox"] = await self._sync_upstox_orders(user_id)
        sync_meta["positions"] = await self._sync_strategy_positions(user_id, kite)
        return sync_meta

    async def _load_orders(self, user_id: str) -> List[Dict[str, Any]]:
        rows = await self._db.orders.find(
            {"user_id": user_id, "visibility": {"$ne": "hidden"}},
            {"_id": 0, "user_id": 0},
        ).sort("created_at", -1).to_list(200)
        return [normalize_order_row(row) for row in rows]

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
        out: List[Dict[str, Any]] = []
        for row in rows:
            risk = row.get("tp_sl_tsl_config") or {}
            stop_loss = risk.get("stoploss_price") or risk.get("stop_loss")
            take_profit = risk.get("target_price") or risk.get("take_profit")
            out.append({
                "id": row.get("id"),
                "strategy_id": row.get("strategy_id"),
                "symbol": row.get("trading_symbol") or row.get("symbol"),
                "exchange": row.get("exchange"),
                "segment": segment_from_exchange(row.get("exchange") or "NSE", row.get("asset_class") or "DIRECT"),
                "instrument_token": row.get("instrument_token"),
                "qty": int(row.get("open_quantity") or row.get("quantity") or 0),
                "avg_price": float(row.get("average_buy_price") or 0),
                "status": row.get("status"),
                "execution_status": row.get("status"),
                "position_side": row.get("position_side") or "LONG",
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "entry_order_id": row.get("entry_order_id"),
                "broker_order_id": row.get("entry_broker_order_id") or row.get("broker_order_id"),
                "mode": row.get("mode") or "live",
            })
        return out

    def _ledger_risk_by_strategy(self) -> Dict[str, Dict[str, Any]]:
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
            try:
                sync_meta = await self.sync_brokers(user_id, user)
            except Exception as exc:
                logger.warning("execution snapshot broker sync failed user=%s: %s", user_id, exc)
                sync_meta = {"error": str(exc)}

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
            "execution_broker": settings.get("execution_broker") or "zerodha",
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
