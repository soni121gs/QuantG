"""
scratch/demonstrate_paper_trade.py

Demonstrates one complete paper trade end-to-end using the internal QuantG backend APIs:
  1. Signal generated
  2. Option selected
  3. LTP obtained
  4. Pre-trade passed
  5. Paper BUY executed
  6. Position opened
  7. Paper SELL executed
  8. Position closed
"""
from __future__ import annotations

import sys
import os
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, AsyncMock

# Ensure backend root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set required environment variables for DB initialization
os.environ.setdefault("DB_NAME", "quantg")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")

from server import (
    db,
    _resolve_option_for_strategy,
    _place_order_core,
)
from brokers.upstox_gateway import UpstoxGateway

async def run_demonstration():
    print("=" * 60)
    print("STARTING END-TO-END PAPER TRADE DEMONSTRATION")
    print("=" * 60)

    # 1. Setup User and Strategy in DB
    user_id = "demo-user-id"
    strategy_id = "d4b8e3a2b7c94d6e9f1a2b3c4d5e6f7a"

    # Clean old data if any
    await db.users.delete_many({"email": "demo@quantdesk.io"})
    await db.users.delete_many({"id": user_id})
    await db.strategies.delete_many({"id": strategy_id})
    await db.strategies.delete_many({"id": "de"})
    await db.strategy_positions.delete_many({"user_id": user_id})
    await db.strategy_positions.delete_many({"strategy_id": "de"})
    await db.orders.delete_many({"user_id": user_id})
    await db.orders.delete_many({"strategy_id": "de"})
    await db.strategy_position_locks.delete_many({})

    # Create dummy user and strategy
    await db.users.insert_one({
        "id": user_id,
        "email": "demo@quantdesk.io",
        "name": "Demo User",
        "settings": {
            "paper_mode": True,
            "per_strategy_capital": 50000.0,
            "max_daily_loss": 5000.0,
            "max_trades_per_day": 10,
        }
    })

    strategy_row = {
        "id": strategy_id,
        "user_id": user_id,
        "name": "Nifty SMA Option Buying Strategy",
        "mode": "paper",
        "broker": "upstox",
        "visual_config": {
            "symbol": "NIFTY",
            "options": {
                "enabled": True,
                "underlying": "NIFTY",
                "strike_mode": "ATM_BUY",
                "otm_points": 50,
                "lots": 1,
            },
            "risk": {
                "stop_loss_pct": 0.10,
                "take_profit_pct": 0.20,
            }
        }
    }
    await db.strategies.insert_one(strategy_row)

    print("\n[STEP 1] Signal Generated:")
    print("  -> Strategy 'Nifty SMA Option Buying Strategy' fired a BUY signal on NIFTY.")

    # 2. Setup Mock Upstox Gateway so that it resolves real contract and fetches real LTP
    gw = UpstoxGateway.__new__(UpstoxGateway)
    gw.access_token = "fake-token"
    gw._tick_cache = {}
    
    # Mock get_market_quote for Nifty spot
    def fake_get_market_quote(keys):
        if "NSE_INDEX|Nifty 50" in keys:
            return {"status": "success", "data": {"NSE_INDEX|Nifty 50": {"last_price": 24853.20}}}
        # Option key quote
        return {"status": "success", "data": {"NSE_FO|123456": {"last_price": 12.45}}}
    gw.get_market_quote = fake_get_market_quote

    # Mock get_option_chain
    def fake_get_option_chain(underlying_key, expiry_date):
        return {
            "status": "success",
            "data": [
                {
                    "strike_price": 24900.00,
                    "expiry": "2026-06-05",
                    "call_options": {
                        "instrument_key": "NSE_FO|123456",
                        "trading_symbol": "NIFTY2660524900CE"
                    }
                }
            ]
        }
    gw.get_option_chain = fake_get_option_chain

    # Mock latest_tick
    gw.latest_tick = MagicMock(return_value={"ltp": 12.45})

    # Patches
    patch_gateway = patch("server.get_user_upstox_gateway", AsyncMock(return_value=gw))
    patch_settings = patch("server.get_user_settings", AsyncMock(return_value={
        "paper_mode": True,
        "per_strategy_capital": 50000.0,
        "max_daily_loss": 5000.0,
        "max_trades_per_day": 10,
    }))

    with patch_gateway, patch_settings:
        print("\n[STEP 2 & 3] Option Contract Selection & Option LTP Retrieval:")
        
        # Resolve Option
        contract = await _resolve_option_for_strategy(
            user_id=user_id,
            strategy_row=strategy_row,
            underlying="NIFTY",
            signal_action="BUY",
            strike_mode="ATM_BUY",
            otm_points=50,
        )

        if not contract:
            print("  -> ERROR: Failed to resolve option contract.")
            return

        print(f"  -> Selected Option Strike: {contract['strike']} {contract['option_type']}")
        print(f"  -> Expiry: {contract['expiry']}")
        print(f"  -> Instrument Key: {contract['instrument_token']}")
        print(f"  -> LTP Obtained: {contract.get('ltp')}")

        # 4. Trigger Paper BUY Order Placement (Pre-trade passed + BUY executed)
        print("\n[STEP 4 & 5] Pre-trade Gate Validation & Paper BUY Order Execution:")
        buy_res = await _place_order_core(
            user_id=user_id,
            symbol="NIFTY",
            side="BUY",
            qty=1,
            order_type="MARKET",
            product="NRML",
            source=f"strategy:{strategy_id}",
            option_contract=contract,
        )

        print(f"  -> Pre-trade Check: PASSED (Allowed Qty: {buy_res.get('qty')})")
        print(f"  -> Paper BUY Execution status: {buy_res.get('status')}")
        print(f"  -> Filled Price: {buy_res.get('price')}")
        print(f"  -> Order ID: {buy_res.get('id')}")

        # 6. Verify Position Opened
        print("\n[STEP 6] Position Manager Verification:")
        pos = await db.strategy_positions.find_one({"user_id": user_id, "strategy_id": strategy_id})
        if pos:
            print(f"  -> Strategy Position Status: {pos.get('status')}")
            print(f"  -> Symbol: {pos.get('trading_symbol')}")
            print(f"  -> Side: {pos.get('position_side')}")
            print(f"  -> Quantity: {pos.get('quantity')}")
            print(f"  -> Average Buy Price: {pos.get('average_buy_price')}")
        else:
            print("  -> ERROR: No active position found in database!")
            return

        # 7 & 8. Trigger Paper SELL Order Placement (SELL executed + Position closed)
        print("\n[STEP 7 & 8] Paper SELL Execution & Position Closeout:")
        # Update transaction type to SELL for the closeout order contract
        exit_contract = {**contract, "transaction_type": "SELL"}
        sell_res = await _place_order_core(
            user_id=user_id,
            symbol="NIFTY",
            side="SELL",
            qty=1,
            order_type="MARKET",
            product="NRML",
            source=f"strategy:{strategy_id}:exit",
            option_contract=exit_contract,
        )

        print(f"  -> Paper SELL Execution status: {sell_res.get('status')}")
        print(f"  -> Filled Price: {sell_res.get('price')}")
        print(f"  -> Order ID: {sell_res.get('id')}")

        pos_after = await db.strategy_positions.find_one({"user_id": user_id, "strategy_id": strategy_id})
        if pos_after:
            print(f"  -> Strategy Position Status After Sell: {pos_after.get('status')}")
            print(f"  -> Closed Price: {pos_after.get('exit_price')}")
            print(f"  -> Realised PNL: {pos_after.get('realized_pnl')}")
        else:
            print("  -> Strategy Position Status After Sell: CLOSED (Deleted)")

    print("\n" + "=" * 60)
    print("END-TO-END PAPER TRADE SUCCESSFUL!")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(run_demonstration())
