"""Single source of truth for live execution state (positions, orders, SL/TP)."""
from __future__ import annotations

import asyncio
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


DEFAULT_STOP_LOSS_PCT = None
DEFAULT_TAKE_PROFIT_PCT = None


def _pct(row: Dict[str, Any], *keys: str, default: Optional[float]) -> Optional[float]:
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
        if stop_pct is not None:
            stop_loss = entry * (1 - stop_pct / 100) if side_text != "SHORT" else entry * (1 + stop_pct / 100)
    if take_profit in (None, ""):
        target_pct = _pct(risk, "take_profit_pct", "target_pct", default=DEFAULT_TAKE_PROFIT_PCT)
        if target_pct is not None:
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
        self._sync_upstox_orders: Optional[Callable[[str], Awaitable[dict]]] = None
        self._sync_strategy_positions: Optional[Callable[[str], Awaitable[dict]]] = None
        self._fetch_positions: Optional[Any] = None
        self._option_ledger: Any = None
        self._last_sync_by_user: Dict[str, float] = {}

    def configure(
        self,
        *,
        db: Any,
        get_user_settings: Callable[[str], Awaitable[dict]],
        sync_upstox_orders: Callable[[str], Awaitable[dict]],
        sync_strategy_positions: Callable[[str], Awaitable[dict]],
        fetch_positions: Any,
        option_ledger: Any,
    ) -> None:
        self._db = db
        self._get_user_settings = get_user_settings
        self._sync_upstox_orders = sync_upstox_orders
        self._sync_strategy_positions = sync_strategy_positions
        self._fetch_positions = fetch_positions
        self._option_ledger = option_ledger

    async def sync_brokers(self, user_id: str, user: dict) -> Dict[str, Any]:
        sync_meta: Dict[str, Any] = {"upstox": {}, "positions": {}}
        sync_meta["upstox"] = await self._sync_upstox_orders(user_id)
        sync_meta["positions"] = await self._sync_strategy_positions(user_id)
        return sync_meta

    async def _load_orders(self, user_id: str, *, paper_mode: bool, today_start: Optional[str] = None, today_end: Optional[str] = None) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {"user_id": user_id, "visibility": {"$ne": "hidden"}}
        if paper_mode and today_start and today_end:
            query["$or"] = [
                {"mode": {"$ne": "paper"}},
                {"mode": "paper", "created_at": {"$gte": today_start, "$lt": today_end}},
                {"mode": "paper", "status": {"$in": list(OPEN_ORDER_STATUSES)}},
            ]
        rows = await self._db.orders.find(
            query,
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

    async def _load_day_realized_pnl(self, user_id: str) -> Dict[str, float]:
        """Canonical realized P&L for today, sourced from db.trade_fills.

        This is the SAME source and trading-day window as
        portfolio_ledger.get_strategy_pnl_today — the single source of truth.
        We deliberately do NOT recompute P&L by summing order docs (which used
        gross_pnl/realized_pnl/pnl fallback chains that produced phantom
        numbers when different fill engines populated different fields).

        Both fill engines (PortfolioLedger and the legacy paper applier) write
        `realized_pnl`, `charges`, `action` and `created_at` to trade_fills, so
        a single aggregation here is complete and consistent.
        """
        from core.portfolio_ledger import _trading_day_window_utc

        start, end = _trading_day_window_utc()
        fills = await self._db.trade_fills.find(
            {
                "user_id": user_id,
                "action": {"$in": ["CLOSE", "REDUCE"]},
                "created_at": {"$gte": start, "$lt": end},
            },
            {"_id": 0, "realized_pnl": 1, "charges": 1, "brokerage": 1},
        ).to_list(2000)

        net_realized = round(sum(float(f.get("realized_pnl") or 0) for f in fills), 2)
        charges = round(
            sum(float(f.get("charges") if f.get("charges") is not None else f.get("brokerage") or 0) for f in fills),
            2,
        )
        return {
            "realized_pnl": net_realized,
            "net_pnl": net_realized,
            "charges": charges,
            "gross_pnl": round(net_realized + charges, 2),
        }

    async def _load_skipped_signals(self, user_id: str, since: Optional[str] = None) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {"user_id": user_id, "visibility": {"$ne": "hidden"}}
        if since:
            # Reset daily: only surface skips seen during the current trading day.
            query["last_seen_at"] = {"$gte": since}
        rows = await self._db.skipped_signals.find(
            query,
            {"_id": 0, "user_id": 0},
        ).sort("last_seen_at", -1).to_list(200)
        strategy_ids = {str(row.get("strategy_id")) for row in rows if row.get("strategy_id")}
        if strategy_ids:
            strategies = await self._db.strategies.find(
                {"user_id": user_id, "id": {"$in": list(strategy_ids)}},
                {"_id": 0, "id": 1, "name": 1},
            ).to_list(500)
            name_by_id = {str(s.get("id")): s.get("name") for s in strategies}
            for row in rows:
                sid = str(row.get("strategy_id") or "")
                if sid and name_by_id.get(sid):
                    row["strategy_name"] = name_by_id[sid]
        return rows

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
            _raw_ltp = row.get("last_ltp")
            try:
                db_ltp = float(_raw_ltp) if _raw_ltp and _raw_ltp != "LTP_UNAVAILABLE" else None
            except (TypeError, ValueError):
                db_ltp = None
            db_pnl = float(row.get("unrealized_pnl") or 0) if row.get("unrealized_pnl") is not None else None
            # Phase 2 #5: credit/debit spreads are one position with two legs. The value
            # to close is short_ltp - long_ltp or long_ltp - short_ltp (stored as spread_value);
            # Map TP/SL to the spread value levels so the existing Positions table renders sensibly.
            structure = row.get("structure")
            is_spread = structure in ("credit_spread", "debit_spread")
            # Phase 2 #4/#2: surface entry greeks (δ/θ/IV) + the delta target the
            # selector aimed for, so the UI can show "δ 0.46 (target 0.45)".
            g_entry = row.get("greeks_at_entry") or {}
            greeks_lite = (
                {"delta": g_entry.get("delta"), "theta": g_entry.get("theta"), "iv": g_entry.get("iv")}
                if g_entry else None
            )
            target_delta = g_entry.get("target_delta") if g_entry.get("target_delta") is not None else row.get("target_delta")
            pos_out = {
                "id": row.get("id"),
                "strategy_id": row.get("strategy_id"),
                "strategy_name": name_by_id.get(str(row.get("strategy_id") or "")),
                "symbol": row.get("trading_symbol") or row.get("symbol") or row.get("target_symbol"),
                "exchange": row.get("exchange"),
                "segment": segment_from_exchange(row.get("exchange") or "NSE", row.get("asset_class") or "DIRECT"),
                "instrument_token": row.get("instrument_token"),
                "qty": int(row.get("open_quantity") or row.get("quantity") or 0),
                "avg_price": avg_price,
                "last_ltp": db_ltp,
                "db_unrealized_pnl": db_pnl,
                "status": row.get("status"),
                "execution_status": row.get("status"),
                "position_side": position_side,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "entry_order_id": row.get("entry_order_id"),
                "broker_order_id": row.get("entry_broker_order_id") or row.get("broker_order_id"),
                "mode": row.get("mode") or "paper",  # default to paper — never assume live
                "greeks": greeks_lite,
                "target_delta": target_delta,
            }
            if is_spread:
                pos_out.update({
                    "structure": structure,
                    "symbol": row.get("target_symbol") or pos_out["symbol"],
                    "last_ltp": row.get("spread_value") if row.get("spread_value") is not None else db_ltp,
                    "take_profit": row.get("spread_tp_value"),
                    "stop_loss": row.get("spread_sl_value"),
                    "net_credit": row.get("net_credit"),
                    "net_debit": row.get("net_debit"),
                    "max_loss": row.get("max_loss"),
                    "max_loss_total": row.get("max_loss_total"),
                    "legs": row.get("legs") or [],
                })
            out.append(pos_out)
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
        # Map strategy rows by strategy_id
        strategy_by_id = {str(sp.get("strategy_id")): sp for sp in strategy_rows if sp.get("strategy_id")}
        
        # Also map strategy rows by symbol for fallback
        strategy_by_symbol = {str(sp.get("symbol")).upper(): sp for sp in strategy_rows if sp.get("symbol")}

        merged_positions: List[Dict[str, Any]] = []

        # Track which strategy rows have been merged/matched
        matched_strategy_ids = set()
        matched_symbols = set()

        for bp in broker_positions:
            bp_merged = dict(bp)
            bp_merged["execution_status"] = bp_merged.get("execution_status") or "FILLED"
            
            symbol = str(bp.get("symbol") or "").upper()
            bp_strategy_id = bp.get("strategy_id")

            # Try to find a matching strategy row
            sp = None
            if bp_strategy_id and str(bp_strategy_id) in strategy_by_id:
                sp = strategy_by_id[str(bp_strategy_id)]
                matched_strategy_ids.add(str(bp_strategy_id))
            elif symbol in strategy_by_symbol:
                sp = strategy_by_symbol[symbol]
                matched_symbols.add(symbol)

            if sp:
                # We have a matching strategy!
                ledger = ledger_risk.get(sp.get("strategy_id") or "", {})
                stop_loss = sp.get("stop_loss") or ledger.get("stop_loss")
                take_profit = sp.get("take_profit") or ledger.get("take_profit")
                trailing_sl = ledger.get("trailing_sl")

                bp_merged.update({
                    "strategy_id": sp.get("strategy_id"),
                    "strategy_name": sp.get("strategy_name"),
                    "strategy_position_id": sp.get("id"),
                    "execution_status": sp.get("execution_status") or sp.get("status") or bp_merged.get("execution_status"),
                    "position_side": sp.get("position_side") or ledger.get("position_side") or "LONG",
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "trailing_sl": trailing_sl,
                    "ledger_status": ledger.get("position_status") or "ACTIVE",
                    "entry_order_id": sp.get("entry_order_id"),
                    "broker_order_id": sp.get("broker_order_id"),
                    "structure": sp.get("structure"),
                    "net_credit": sp.get("net_credit"),
                    "max_loss": sp.get("max_loss"),
                    "legs": sp.get("legs"),
                    "greeks": sp.get("greeks"),
                    "target_delta": sp.get("target_delta"),
                })
                if ledger.get("ltp") is not None:
                    bp_merged["ltp"] = ledger.get("ltp")
                elif sp.get("last_ltp"):
                    bp_merged["ltp"] = sp.get("last_ltp")
                if ledger.get("unrealized_pnl") is not None:
                    bp_merged["pnl"] = ledger.get("unrealized_pnl")
                elif sp.get("db_unrealized_pnl") is not None:
                    bp_merged["pnl"] = sp.get("db_unrealized_pnl")
            else:
                # No active strategy row was matched! This is an orphan position.
                ledger = {}
                if bp_strategy_id:
                    ledger = ledger_risk.get(str(bp_strategy_id), {})

                stop_loss = bp.get("stop_loss") or ledger.get("stop_loss")
                take_profit = bp.get("take_profit") or ledger.get("take_profit")
                trailing_sl = ledger.get("trailing_sl")

                bp_merged.update({
                    "strategy_id": bp_strategy_id,
                    "strategy_name": bp.get("strategy_name") or (f"Orphan ({bp_strategy_id[:8]})" if bp_strategy_id else "Orphan/Stale"),
                    "strategy_position_id": None,
                    "execution_status": "ORPHAN",
                    "ledger_status": ledger.get("position_status") or "ORPHAN",
                    "position_side": bp.get("position_side") or ledger.get("position_side") or "LONG",
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "trailing_sl": trailing_sl,
                })
                if ledger.get("ltp") is not None:
                    bp_merged["ltp"] = ledger.get("ltp")
                if ledger.get("unrealized_pnl") is not None:
                    bp_merged["pnl"] = ledger.get("unrealized_pnl")

            merged_positions.append(bp_merged)

        # Now, check if there are any strategy rows that were not matched by any broker position
        for sp in strategy_rows:
            sp_sid = str(sp.get("strategy_id") or "")
            sp_symbol = str(sp.get("symbol") or "").upper()
            if sp_sid in matched_strategy_ids or sp_symbol in matched_symbols:
                continue

            ledger = ledger_risk.get(sp.get("strategy_id") or "", {})
            stop_loss = sp.get("stop_loss") or ledger.get("stop_loss")
            take_profit = sp.get("take_profit") or ledger.get("take_profit")
            trailing_sl = ledger.get("trailing_sl")

            merged_positions.append({
                "symbol": sp_symbol,
                "qty": sp.get("qty") or 0,
                "avg_price": sp.get("avg_price") or ledger.get("entry_price") or 0,
                "ltp": ledger.get("ltp") or sp.get("last_ltp") or sp.get("avg_price") or 0,
                "pnl": ledger.get("unrealized_pnl") if ledger.get("unrealized_pnl") is not None else (sp.get("db_unrealized_pnl") if sp.get("db_unrealized_pnl") is not None else 0),
                "mode": sp.get("mode") or "paper",
                "exchange": sp.get("exchange"),
                "segment": sp.get("segment"),
                "instrument_token": sp.get("instrument_token"),
                "broker": None,
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
                # Phase 2 #5: carry credit-spread fields through to the UI.
                "structure": sp.get("structure"),
                "net_credit": sp.get("net_credit"),
                "max_loss": sp.get("max_loss"),
                "max_loss_total": sp.get("max_loss_total"),
                "legs": sp.get("legs"),
                "greeks": sp.get("greeks"),
                "target_delta": sp.get("target_delta"),
            })

        return merged_positions

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

        today = settings.get("today_window_ist") or {}
        (
            broker_positions,
            strategy_rows,
            orders,
            skipped_signals,
            wallet_row_raw,
            feed_state_raw,
            active_count,
            total_count,
            scanning_count,
            day_pnl,
        ) = await asyncio.gather(
            self._fetch_positions(user, settings),
            self._load_strategy_positions(user_id),
            self._load_orders(
                user_id,
                paper_mode=bool(settings.get("paper_mode", True)),
                today_start=today.get("start"),
                today_end=today.get("end"),
            ),
            self._load_skipped_signals(user_id, today.get("start")),
            self._db.paper_wallets.find_one({"user_id": user_id}, {"_id": 0}),
            self._db.upstox_reconciliation_state.find_one({"_id": "latest"}, {"_id": 0}),
            self._db.strategies.count_documents({"user_id": user_id, "status": "live"}),
            self._db.strategies.count_documents({"user_id": user_id}),
            self._db.signals.count_documents({"user_id": user_id, "status": "PENDING"}),
            self._load_day_realized_pnl(user_id),
        )
        wallet_row = wallet_row_raw or {}
        feed_state = feed_state_raw or {}
        strategy_state = {
            "active": active_count,
            "total": total_count,
            "scanning": scanning_count,
        }

        ledger_risk = self._ledger_risk_by_strategy()
        positions = self._merge_position_risk(broker_positions, strategy_rows, ledger_risk)

        open_orders = [o for o in orders if o.get("status") in OPEN_ORDER_STATUSES]
        failed_orders = [o for o in orders if o.get("status") == "FAILED"]

        # Calculate position integrity health metrics for the dashboard
        broker_active = [p for p in positions if int(p.get("qty") or 0) != 0]
        active_sp_symbols = {str(sp.get("symbol")).upper() for sp in strategy_rows if sp.get("symbol")}
        active_sp_strategy_ids = {str(sp.get("strategy_id")) for sp in strategy_rows if sp.get("strategy_id")}
        
        orphan_positions_count = 0
        missing_sl_count = 0
        missing_tp_count = 0
        strategy_mismatches_count = 0
        
        for bp in broker_active:
            symbol = str(bp.get("symbol") or "").upper()
            strategy_id = bp.get("strategy_id")
            
            is_orphan = True
            if strategy_id and str(strategy_id) in active_sp_strategy_ids:
                is_orphan = False
            elif symbol in active_sp_symbols:
                is_orphan = False
                
            if is_orphan:
                orphan_positions_count += 1
                
        for sp in strategy_rows:
            symbol = str(sp.get("symbol") or "").upper()
            
            has_sl = sp.get("stop_loss") is not None
            has_tp = sp.get("take_profit") is not None
            
            if not has_sl:
                missing_sl_count += 1
            if not has_tp:
                missing_tp_count += 1
                
            bp_match = next((p for p in broker_active if str(p.get("symbol")).upper() == symbol), None)
            if not bp_match:
                strategy_mismatches_count += 1
            else:
                bp_qty = abs(int(bp_match.get("qty") or 0))
                sp_qty = int(sp.get("qty") or 0)
                if bp_qty != sp_qty:
                    strategy_mismatches_count += 1

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "paper_mode": bool(settings.get("paper_mode", True)),
            "execution_broker": "upstox",
            "sync": sync_meta,
            "wallet": wallet_row,
            "feed_state": feed_state,
            "strategy_state": strategy_state,
            "positions": positions,
            "orders": orders,
            "skipped_signals": skipped_signals,
            "open_orders": open_orders,
            "failed_orders": failed_orders,
            "strategy_positions": strategy_rows,
            "ledger_strategies": ledger_risk,
            "summary": {
                "open_positions": len([p for p in positions if int(p.get("qty") or 0) != 0]),
                "open_orders": len(open_orders),
                "failed_orders": len(failed_orders),
                "skipped_signals": sum(int(row.get("count") or 1) for row in skipped_signals),
                "total_unrealized_pnl": round(sum(float(p.get("pnl") or 0) for p in positions), 2),
                # Canonical realized P&L from db.trade_fills (single source of truth).
                # NOT recomputed by summing order docs — that produced phantom
                # numbers via gross_pnl/realized_pnl/pnl fallback chains.
                "gross_pnl": day_pnl["gross_pnl"],
                "charges": day_pnl["charges"],
                "net_pnl": day_pnl["net_pnl"],
                "realized_pnl": day_pnl["realized_pnl"],
                # Account-level daily-loss kill switch state, synced to the
                # Trading Preferences MAX DAILY LOSS (settings.max_daily_loss).
                # Same realized-P&L basis the entry guard enforces, so the
                # cockpit and the actual kill switch never disagree.
                "account_daily_loss_limit": round(float(settings.get("max_daily_loss") or 0), 2),
                "account_daily_loss_armed": float(settings.get("max_daily_loss") or 0) > 0,
                "account_daily_loss_breached": (
                    float(settings.get("max_daily_loss") or 0) > 0
                    and float(day_pnl["realized_pnl"]) <= -abs(float(settings.get("max_daily_loss") or 0))
                ),
                "position_integrity": {
                    "orphans": orphan_positions_count,
                    "missing_sl": missing_sl_count,
                    "missing_tp": missing_tp_count,
                    "strategy_mismatches": strategy_mismatches_count,
                    "failed_orders": len(failed_orders),
                }
            },
        }


execution_state_manager = ExecutionStateManager()
