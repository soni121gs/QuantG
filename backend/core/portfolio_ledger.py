"""QuantG Portfolio Ledger.

One source of truth for all positions (paper, live, backtest).
Enforces: No fill = no position. A position only exists after a fill is recorded.
"""
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import uuid
import logging

logger = logging.getLogger("quantg.portfolio_ledger")

class PortfolioLedger:
    def __init__(self, db):
        self.db = db

    async def process_fill(self, fill: Dict[str, Any]) -> None:
        """Processes a trade fill, updating or closing corresponding strategy and portfolio positions.
        
        Enforces transaction-by-transaction inventory ledger mapping.
        """
        user_id = fill["user_id"]
        strategy_id = fill["strategy_id"]
        target_symbol = fill["target_symbol"]
        side = fill["side"]
        qty = int(fill["qty"])
        price = float(fill["price"])
        mode = fill["mode"]
        now_str = datetime.now(timezone.utc).isoformat()
        stop_loss = fill.get("stop_loss")
        take_profit = fill.get("take_profit")
        protection = {
            "stoploss_price": stop_loss,
            "stop_loss": stop_loss,
            "target_price": take_profit,
            "take_profit": take_profit,
            "protection_status": "PROTECTED" if stop_loss or take_profit else "UNPROTECTED",
        }

        # Group positions by strategy and mode
        pos = await self.db.strategy_positions.find_one({
            "user_id": user_id,
            "strategy_id": strategy_id,
            "target_symbol": target_symbol,
            "mode": mode,
            "status": "OPEN"
        })

        if not pos:
            # Guard: a SELL fill with no matching OPEN position could be a duplicate
            # exit fill (the first exit already closed the LONG position). Creating a
            # new SHORT position from that fill would be wrong — it accumulates phantom
            # short exposure from every redundant exit order.
            if side == "SELL":
                closed_long = await self.db.strategy_positions.find_one({
                    "user_id": user_id,
                    "strategy_id": strategy_id,
                    "target_symbol": target_symbol,
                    "mode": mode,
                    "position_side": "LONG",
                    "status": {"$in": ["CLOSED", "EXITING"]},
                })
                if closed_long:
                    logger.warning(
                        "Ledger: SELL fill for %s (strategy=%s) found no OPEN LONG but a "
                        "CLOSED/EXITING one exists — duplicate exit fill, skipping.",
                        target_symbol, strategy_id,
                    )
                    return

            # Create a brand new position (No pre-existing open state)
            pos_id = f"pos_{uuid.uuid4().hex[:12]}"
            position_doc = {
                "id": pos_id,
                "user_id": user_id,
                "strategy_id": strategy_id,
                "symbol": fill["symbol"],
                "target_symbol": target_symbol,
                "trading_symbol": target_symbol,
                "instrument_token": fill.get("instrument_token"),
                "mode": mode,
                "quantity": qty,
                "open_quantity": qty,
                "average_price": price,
                "average_buy_price": price,
                "position_side": "LONG" if side == "BUY" else "SHORT",
                "status": "OPEN",
                "realised_pnl": 0.0,
                "unrealised_pnl": 0.0,
                "pnl": 0.0,
                "brokerage": float(fill.get("brokerage", 0.0)),
                "created_at": now_str,
                "updated_at": now_str,
                "source_fill_id": fill["id"],
                "tp_sl_tsl_config": protection,
            }
            await self.db.strategy_positions.insert_one(position_doc)
            
            # Mirror standard portfolio position (keyed by strategy_id to avoid multi-strategy overwrite)
            await self.db.positions.update_one(
                {"user_id": user_id, "symbol": target_symbol, "mode": mode, "strategy_id": strategy_id},
                {
                    "$set": {
                        "user_id": user_id,
                        "strategy_id": strategy_id,
                        "symbol": target_symbol,
                        "target_symbol": target_symbol,
                        "underlying": fill["symbol"],
                        "mode": mode,
                        "quantity": qty,
                        "open_quantity": qty,
                        "qty": qty,
                        "average_buy_price": price,
                        "avg_price": price,
                        "stop_loss": stop_loss,
                        "take_profit": take_profit,
                        "tp_sl_tsl_config": protection,
                        "updated_at": now_str
                    }
                },
                upsert=True
            )
            logger.info(f"Portfolio ledger created NEW position {pos_id} for strategy {strategy_id} symbol {target_symbol}")
        else:
            # Position exists: Check if it's adding or exiting
            pos_side = pos["position_side"]
            is_entry = (pos_side == "LONG" and side == "BUY") or (pos_side == "SHORT" and side == "SELL")
            
            if is_entry:
                # Add to existing position (average up/down)
                new_qty = pos["open_quantity"] + qty
                pos_avg_price = float(pos.get("average_price") or pos.get("average_buy_price") or pos.get("avg_price") or 0)
                avg_price = ((pos_avg_price * pos["open_quantity"]) + (price * qty)) / new_qty
                
                await self.db.strategy_positions.update_one(
                    {"id": pos["id"]},
                    {
                        "$set": {
                            "open_quantity": new_qty,
                            "quantity": new_qty,
                            "average_price": avg_price,
                            "average_buy_price": avg_price,
                            "updated_at": now_str
                        },
                        "$inc": {
                            "brokerage": float(fill.get("brokerage", 0.0))
                        }
                    }
                )
                
                await self.db.positions.update_one(
                    {"user_id": user_id, "symbol": target_symbol, "mode": mode, "strategy_id": strategy_id},
                    {
                        "$set": {
                            "strategy_id": strategy_id,
                            "symbol": target_symbol,
                            "target_symbol": target_symbol,
                            "underlying": fill["symbol"],
                            "mode": mode,
                            "quantity": new_qty,
                            "open_quantity": new_qty,
                            "qty": new_qty,
                            "average_buy_price": avg_price,
                            "avg_price": avg_price,
                            "stop_loss": stop_loss,
                            "take_profit": take_profit,
                            "tp_sl_tsl_config": protection,
                            "updated_at": now_str
                        }
                    }
                )
                logger.info(f"Portfolio ledger averaged existing position {pos['id']} to qty {new_qty}")
            else:
                # Opposite transaction: Exiting or partially exiting
                exit_qty = min(qty, pos["open_quantity"])
                remaining_qty = pos["open_quantity"] - exit_qty
                
                # P&L Calculation
                pnl = 0.0
                pos_avg_price = float(pos.get("average_price") or pos.get("average_buy_price") or pos.get("avg_price") or 0)
                if pos_side == "LONG":
                    pnl = (price - pos_avg_price) * exit_qty
                else:
                    pnl = (pos_avg_price - price) * exit_qty
                    
                # Deduct brokerages
                net_pnl = pnl - float(fill.get("brokerage", 0.0))
                
                update_fields = {
                    "open_quantity": remaining_qty,
                    "updated_at": now_str,
                }
                
                if remaining_qty <= 0:
                    update_fields["status"] = "CLOSED"
                    update_fields["closed_at"] = now_str
                    
                await self.db.strategy_positions.update_one(
                    {"id": pos["id"]},
                    {
                        "$set": update_fields,
                        "$inc": {
                            "realised_pnl": net_pnl,
                            "pnl": net_pnl,
                            "brokerage": float(fill.get("brokerage", 0.0))
                        }
                    }
                )
                
                # Mirror standard portfolio position closure
                if remaining_qty <= 0:
                    await self.db.positions.delete_one({"user_id": user_id, "symbol": target_symbol, "mode": mode, "strategy_id": strategy_id})
                else:
                    await self.db.positions.update_one(
                        {"user_id": user_id, "symbol": target_symbol, "mode": mode, "strategy_id": strategy_id},
                        {
                            "$set": {
                                "quantity": remaining_qty,
                                "open_quantity": remaining_qty,
                                "qty": remaining_qty,
                                "updated_at": now_str
                            }
                        }
                    )
                    
                # Increment Strategy scorecard P&L
                await self.db.strategies.update_one(
                    {"id": strategy_id, "user_id": user_id},
                    {
                        "$set": {
                            "last_pnl": net_pnl
                        },
                        "$inc": {
                            "today_pnl": net_pnl
                        }
                    }
                )
                logger.info(f"Portfolio ledger closed/reduced position {pos['id']}. Realized P&L: {net_pnl}")
