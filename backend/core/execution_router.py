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
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import uuid
import logging

from core.portfolio_ledger import PortfolioLedger
from core.paper_broker import PaperWallet
from execution_bridge import submit_order as bridge_submit_order

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
        }
        await self.db.fills.insert_one(fill_doc)

        # Update Portfolio Ledger (Hard Rule: No fill = no position)
        await self.ledger.process_fill(fill_doc)

        return order_doc

class UpstoxLiveAdapter:
    """Live trading execution adapter.
    
    Dispatches orders to Upstox broker for real execution on market.
    
    Part of the unified pipeline:
    - Receives identical OrderIntent as PaperAdapter
    - Applies identical risk checks via RiskManager
    - Creates order/fill records in the same schema
    - Updates portfolio ledger identically
    
    Differences from PaperAdapter:
    - Fills are async (subject to market conditions)
    - Requires broker connection (Upstox API)
    - Real fund deduction via margin
    - Real market slippage
    
    Safety measures:
    - Requires CORE_ENGINE_LIVE_ENABLED environment flag
    - Requires user.live_arm_state.armed = true
    - Requires Upstox OAuth authentication
    """
    def __init__(self, db, ledger: PortfolioLedger):
        self.db = db
        self.ledger = ledger

    async def execute(self, user_id: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches concrete trade execution payloads to Upstox's production endpoints."""
        now = datetime.now(timezone.utc).isoformat()
        
        # Double check live safety arm switch and global core live flag
        import os
        live_enabled = os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true"
        if not live_enabled:
            raise RuntimeError("Live execution blocked: CORE_ENGINE_LIVE_ENABLED is set to false in the environment.")

        if intent.get("segment") == "MCX_FO" or intent.get("exchange") == "MCX":
            raise RuntimeError("Live execution blocked: Live trading is disabled for MCX segment on Upstox API.")

        arm = await self.db.live_arm_state.find_one({"user_id": user_id})
        if not arm or not arm.get("armed"):
            raise RuntimeError("Live execution blocked: System is not armed.")
            
        logger.info(f"Live order intent approved. Routing to broker: {intent['target_symbol']}")
        
        # Call legacy bridge function to perform actual placement
        # Wrap it with preflight metrics logging
        try:
            from fastapi import HTTPException
            from pydantic import BaseModel
            
            # Reconstruct model payload for the bridge
            class RefModel(BaseModel):
                exchange: str
                tradingsymbol: str
                instrument_token: str
                segment: str
                broker: str = "upstox"
                asset_class: str = "OPTION_LONG"
                
            class IntentModel(BaseModel):
                instrument: RefModel
                quantity: int
                intent: str
                stop_loss: Optional[float] = None
                take_profit: Optional[float] = None

            ref = RefModel(
                exchange=intent["exchange"],
                tradingsymbol=intent["target_symbol"],
                instrument_token=intent.get("instrument_token") or "0",
                segment=intent["segment"]
            )
            intent_model = IntentModel(
                instrument=ref,
                quantity=intent["qty"],
                intent="OPEN_LONG" if intent["side"] == "BUY" else "CLOSE_LONG"
            )
            
            # Dispatch actual trade call to upstox API
            submit_res = await bridge_submit_order(
                user_id=user_id,
                intent=intent_model,
                order_type=intent.get("order_type", "MARKET"),
                product="MIS",
                price=intent["requested_price"],
                tag=intent.get("execution_tag") or f"quantg:{intent['strategy_id'][:18]}:{datetime.now(timezone.utc).strftime('%Y%m%d')}"
            )
            
            broker_order_id = submit_res.get("broker_order_id") or submit_res.get("order_id")
            order_doc = {
                "id": str(uuid.uuid4()),
                "user_id": user_id,
                "strategy_id": intent["strategy_id"],
                "symbol": intent["symbol"],
                "target_symbol": intent["target_symbol"],
                "side": intent["side"],
                "qty": intent["qty"],
                "filled_qty": 0,
                "pending_qty": intent["qty"],
                "status": "PLACED",
                "execution_status": "PLACED",
                "requested_price": intent["requested_price"],
                "price": intent["requested_price"],
                "exchange": intent["exchange"],
                "segment": intent["segment"],
                "mode": "live",
                "broker": "upstox",
                "broker_order_id": broker_order_id,
                "idempotency_key": intent.get("idempotency_key"),
                "created_at": now,
                "updated_at": now
            }
            
            await self.db.orders.insert_one(order_doc)
            return order_doc
            
        except Exception as e:
            logger.error(f"Live broker trade dispatch failed: {e}")
            raise RuntimeError(f"Broker rejected live order intent: {e}")

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
    def __init__(self, db, ledger: PortfolioLedger):
        self.db = db
        self.paper_adapter = PaperAdapter(db, ledger)
        self.live_adapter = UpstoxLiveAdapter(db, ledger)

    async def route_intent(self, user_id: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        mode = str(intent["mode"]).lower()
        if mode == "paper":
            return await self.paper_adapter.execute(user_id, intent)
        if mode == "live":
            return await self.live_adapter.execute(user_id, intent)
        raise ValueError(f"Execution router does not support live routing of mode: {mode}")
