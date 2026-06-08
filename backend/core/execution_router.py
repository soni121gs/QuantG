"""QuantG Execution Router.

Routes OrderIntents to the correct adapter: PaperAdapter or UpstoxLiveAdapter.

Unified Pipeline Design:
- IDENTICAL risk checks (via RiskManager) applied to both paper and live modes
- IDENTICAL preflight validation (via ReadinessChecker) 
- ONLY execution differs: paper uses virtual wallet, live uses Upstox API

The adapters are designed to be fully interchangeable at execution time:
- PaperAdapter: Simulates fills using paper wallet (instant, deterministic)
- UpstoxLiveAdapter: Dispatches to Upstox broker API (async, subject to market)

Both adapters:
1. Create order documents in the database
2. Generate fill records
3. Update portfolio ledger
4. Support identical OrderIntent schema

This ensures strategies can trade paper and live without code changes—only the
mode flag changes where the order goes.
"""
from typing import Dict, Any, Callable, Awaitable, Optional
from datetime import datetime, timezone
import os
import uuid
import logging

from core.portfolio_ledger import PortfolioLedger
from core.paper_broker import PaperWallet
from execution_bridge import submit_order as bridge_submit_order, ExecutionDispatchPayload

logger = logging.getLogger("quantg.execution_router")

class PaperAdapter:
    """Paper trading execution adapter.
    
    Executes trades against a virtual paper wallet with simulated slippage.
    Used for backtesting and paper trading.
    
    Part of the unified pipeline:
    - Receives identical OrderIntent as UpstoxLiveAdapter
    - Applies identical risk checks via RiskManager
    - Creates order/fill records in the same schema
    - Updates portfolio ledger identically
    
    Differences from UpstoxLiveAdapter:
    - Fills are instant (not async)
    - No broker interaction (deterministic)
    - Uses virtual wallet for fund management
    - Simulated slippage: 0.02% on price, 0.05% brokerage
    """
    def __init__(self, db, ledger: PortfolioLedger):
        self.db = db
        self.ledger = ledger
        self.wallet = PaperWallet(db)

    async def execute(self, user_id: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Simulates immediate fill using virtual paper wallet money.

        - BUY: debit wallet by fill_price × qty (rejected if balance < cost)
        - SELL: credit wallet by fill_price × qty
        - Slippage and brokerage are also deducted from the wallet.
        """
        now = datetime.now(timezone.utc).isoformat()
        fill_price = float(intent["requested_price"])
        qty = int(intent["qty"])
        side = str(intent["side"]).upper()

        # Simulated Upstox-like slippage and charges. The main server order
        # path uses broker charge APIs in live mode and the same accounting
        # shape in paper mode; keep this optional core path consistent.
        slippage = round(fill_price * 0.00035, 2)
        final_price = round(
            fill_price + slippage if side == "BUY" else fill_price - slippage, 2
        )
        gross = abs(final_price * qty)
        brokerage = round(min(20.0, gross * 0.0003), 2)
        stt = round(gross * (0.000625 if side == "SELL" else 0.0), 2)
        exchange_txn = round(gross * 0.00053, 2)
        sebi = round(gross * 0.000001, 2)
        stamp = round(gross * (0.00003 if side == "BUY" else 0.0), 2)
        gst = round((brokerage + exchange_txn + sebi) * 0.18, 2)
        charges = round(brokerage + stt + exchange_txn + sebi + stamp + gst, 2)

        order_id = intent.get("id") or str(uuid.uuid4())
        trade_value = round(final_price * qty, 2)

        # ------------------------------------------------------------------
        # Paper wallet balance check & update
        # ------------------------------------------------------------------
        if side == "BUY":
            total_cost = round(trade_value + charges, 2)
            balance_ok = await self.wallet.debit(user_id, total_cost, order_id)
            if not balance_ok:
                current_balance = await self.wallet.get_balance(user_id)
                raise ValueError(
                    f"Insufficient paper funds: need ₹{total_cost:,.2f}, "
                    f"have ₹{current_balance:,.2f}. "
                    f"Reset paper account to restore ₹5,00,000."
                )
        else:  # SELL / exit: credit back proceeds
            proceeds = round(trade_value - charges, 2)
            await self.wallet.credit(user_id, max(0.0, proceeds), order_id)

        order_doc = {
            "id": order_id,
            "user_id": user_id,
            "strategy_id": intent["strategy_id"],
            "symbol": intent["symbol"],
            "target_symbol": intent["target_symbol"],
            "side": side,
            "qty": qty,
            "filled_qty": qty,
            "pending_qty": 0,
            "status": "FILLED",
            "execution_status": "FILLED",
            "requested_price": fill_price,
            "price": final_price,
            "brokerage": brokerage,
            "charges": charges,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "slippage": slippage,
            "trade_value": trade_value,
            "exchange": intent["exchange"],
            "segment": intent["segment"],
            "mode": "paper",
            "broker": "paper",
            "execution_tag": intent.get("execution_tag") or f"quantg:{intent['strategy_id'][:18]}:{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            "paper_realism": "UPSTOX_LIKE",
            "paper_fill_applied": True,
            "legacy_status": "COMPLETE",
            "status_message": "FILLED: unified core paper fill applied",
            "pretrade_cost": {
                "source": "core_paper_upstox_cost_model",
                "estimated_charges": charges,
                "charges_breakup": {
                    "brokerage": brokerage,
                    "stt": stt,
                    "exchange_txn": exchange_txn,
                    "sebi": sebi,
                    "stamp": stamp,
                    "gst": gst,
                    "total": charges,
                },
            },
            "created_at": now,
            "updated_at": now,
            "idempotency_key": intent.get("idempotency_key"),
            "stop_loss": intent.get("stop_loss"),
            "take_profit": intent.get("take_profit"),
        }

        # Write to db
        await self.db.orders.insert_one(order_doc)

        # Write matching fill record
        fill_id = f"fill_{uuid.uuid4().hex[:12]}"
        fill_doc = {
            "id": fill_id,
            "order_id": order_id,
            "strategy_id": intent["strategy_id"],
            "user_id": user_id,
            "symbol": intent["symbol"],
            "target_symbol": intent["target_symbol"],
            "side": side,
            "qty": qty,
            "price": final_price,
            "brokerage": brokerage,
            "charges": charges,
            "net_pnl": 0.0,
            "trade_value": trade_value,
            "mode": "paper",
            "created_at": now,
            "stop_loss": intent.get("stop_loss"),
            "take_profit": intent.get("take_profit"),
            "instrument_token": intent.get("instrument_token") or intent.get("instrument_key"),
        }
        await self.db.fills.insert_one(fill_doc)

        # Update Portfolio Ledger (Hard Rule: No fill = no position)
        await self.ledger.process_fill(fill_doc)

        return order_doc

class UpstoxLiveAdapter:
    """Live trading execution adapter.

    Dispatches orders to Upstox via execution_bridge.submit_order().

    Requires two callbacks injected at construction time by server.py:
      place_upstox_fn      — coroutine that places the actual Upstox order
      resolve_upstox_token_fn — sync fn(exchange, tradingsymbol, token) -> Optional[str]

    Safety gates (in order):
      1. CORE_ENGINE_LIVE_ENABLED env flag
      2. MCX permanently blocked
      3. live_arm_state.armed = true
      4. live_arm_state.global_live_enabled = true
      5. instrument_token valid Upstox key (contains '|', not PAPER_/mock_/"0")
    """

    def __init__(
        self,
        db,
        ledger: PortfolioLedger,
        *,
        place_upstox_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
        resolve_upstox_token_fn: Optional[Callable[[str, str, str], Optional[str]]] = None,
    ):
        self.db = db
        self.ledger = ledger
        self._place_upstox_fn = place_upstox_fn
        self._resolve_upstox_token_fn = resolve_upstox_token_fn

    async def execute(self, user_id: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches a live OrderIntent to Upstox via execution_bridge.submit_order()."""
        now = datetime.now(timezone.utc).isoformat()

        # Gate 1: env flag
        if not os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true":
            raise RuntimeError("Live execution blocked: CORE_ENGINE_LIVE_ENABLED is not true.")

        # Gate 2: MCX permanently blocked
        if intent.get("segment") == "MCX_FO" or intent.get("exchange") == "MCX":
            raise RuntimeError("Execution blocked: MCX has been removed from QuantG.")

        # Gate 3 & 4: arm state
        arm = await self.db.live_arm_state.find_one({"user_id": user_id})
        if not arm or not arm.get("armed"):
            raise RuntimeError("Live execution blocked: live_arm_state.armed is not true.")
        if not arm.get("global_live_enabled"):
            raise RuntimeError("Live execution blocked: live_arm_state.global_live_enabled is not true.")

        # Gate 5: instrument token must be a real Upstox instrument key
        instrument_token = str(intent.get("instrument_token") or "").strip()
        if not instrument_token or instrument_token == "0":
            raise RuntimeError(
                f"Live execution blocked: instrument_token missing or '0' for {intent.get('target_symbol')}."
            )
        if instrument_token.upper().startswith("PAPER_"):
            raise RuntimeError(
                f"Live execution blocked: PAPER_ instrument_key cannot be used: {instrument_token}"
            )
        if instrument_token.lower().startswith("mock_"):
            raise RuntimeError(
                f"Live execution blocked: mock instrument_key cannot be used: {instrument_token}"
            )
        if "|" not in instrument_token:
            raise RuntimeError(
                f"Live execution blocked: instrument_key must contain '|' (Upstox format): {instrument_token}"
            )

        # Callbacks must be injected from server.py when constructing ExecutionRouter
        if self._place_upstox_fn is None:
            raise RuntimeError(
                "CORE_LIVE_ADAPTER_NOT_CONFIGURED: place_upstox_fn not injected. "
                "Pass place_upstox_fn=_place_upstox_order when constructing ExecutionRouter."
            )

        resolve_fn = self._resolve_upstox_token_fn or (
            lambda exchange, tradingsymbol, tok: tok if "|" in str(tok or "") else None
        )

        # Build correct ExecutionDispatchPayload for execution_bridge
        price_val = intent.get("requested_price")
        payload = ExecutionDispatchPayload(
            broker="upstox",
            segment=str(intent["segment"]),
            exchange=str(intent["exchange"]),
            tradingsymbol=str(intent.get("target_symbol") or intent.get("symbol") or ""),
            instrument_token=instrument_token,
            quantity=int(intent["qty"]),
            order_type=str(intent.get("order_type") or "MARKET").upper(),
            side=str(intent["side"]).upper(),
            product=str(intent.get("product") or "MIS").upper(),
            price=float(price_val) if price_val else None,
            stop_loss=intent.get("stop_loss"),
            take_profit=intent.get("take_profit"),
            tag=intent.get("execution_tag") or f"quantg:{intent['strategy_id'][:18]}:{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            validity="DAY",
        )

        logger.info(
            "Live order dispatching user=%s symbol=%s side=%s qty=%s token=%s",
            user_id, payload.tradingsymbol, payload.side, payload.quantity, instrument_token,
        )

        submit_res = await bridge_submit_order(
            user_id,
            payload,
            place_upstox=self._place_upstox_fn,
            resolve_upstox_token=resolve_fn,
        )

        if not submit_res.get("ok"):
            raise RuntimeError(
                f"Upstox order rejected for {payload.tradingsymbol}: {submit_res.get('error')}"
            )

        broker_order_id = submit_res.get("broker_order_id")
        order_doc = {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "strategy_id": intent["strategy_id"],
            "symbol": intent["symbol"],
            "target_symbol": intent.get("target_symbol", intent["symbol"]),
            "side": intent["side"],
            "qty": intent["qty"],
            "filled_qty": 0,
            "pending_qty": intent["qty"],
            "status": "PLACED",
            "execution_status": "PLACED",
            "requested_price": price_val,
            "price": price_val,
            "exchange": intent["exchange"],
            "segment": intent["segment"],
            "mode": "live",
            "broker": "upstox",
            "broker_order_id": broker_order_id,
            "idempotency_key": intent.get("idempotency_key"),
            "created_at": now,
            "updated_at": now,
        }

        await self.db.orders.insert_one(order_doc)
        return order_doc

class BacktestAdapter:
    def __init__(self, ledger_records: list):
        # In-memory backtest ledger simulator
        self.ledger_records = ledger_records

    def execute(self, intent: Dict[str, Any], timestamp: str) -> Dict[str, Any]:
        """Simulates historical bar execution fills with zero future lookahead."""
        fill_price = float(intent["requested_price"])
        
        # slippage model
        slippage = round(fill_price * 0.0003, 2)
        final_price = round(fill_price + slippage if intent["side"] == "BUY" else fill_price - slippage, 2)
        brokerage = 20.0
        
        fill_doc = {
            "id": f"bt_fill_{uuid.uuid4().hex[:10]}",
            "strategy_id": intent["strategy_id"],
            "symbol": intent["symbol"],
            "target_symbol": intent["target_symbol"],
            "side": intent["side"],
            "qty": intent["qty"],
            "price": final_price,
            "brokerage": brokerage,
            "mode": "backtest",
            "created_at": timestamp
        }
        
        self.ledger_records.append(fill_doc)
        return fill_doc

class ExecutionRouter:
    def __init__(
        self,
        db,
        ledger: PortfolioLedger,
        *,
        place_upstox_fn: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
        resolve_upstox_token_fn: Optional[Callable[[str, str, str], Optional[str]]] = None,
    ):
        self.db = db
        self.paper_adapter = PaperAdapter(db, ledger)
        self.live_adapter = UpstoxLiveAdapter(
            db,
            ledger,
            place_upstox_fn=place_upstox_fn,
            resolve_upstox_token_fn=resolve_upstox_token_fn,
        )

    async def route_intent(self, user_id: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        mode = str(intent["mode"]).lower()
        if mode == "paper":
            return await self.paper_adapter.execute(user_id, intent)
        if mode == "live":
            return await self.live_adapter.execute(user_id, intent)
        raise ValueError(f"Execution router does not support live routing of mode: {mode}")
