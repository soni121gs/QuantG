"""QuantG Portfolio Ledger.

One source of truth for all positions (paper, live, backtest).
Enforces: No fill = no position. A position only exists after a fill is recorded.

This is the accounting SPINE. Every accepted fill produces:
  - a strategy_positions create/update (OPEN → EXITING → CLOSED lifecycle)
  - a db.trade_fills row (immutable audit of every fill applied to a position)
  - on full close, a db.trades row (the round-trip record win-rate / expectancy
    analytics read from)
  - strategy scorecard updates (today_pnl, total_pnl, total_trades, wins, losses)

process_fill returns a result dict so callers (PaperAdapter, live fill poller)
can react — e.g. NOT credit the wallet for a rejected duplicate exit fill:

    {
        "accepted": bool,
        "action":   "OPEN" | "ADD" | "REDUCE" | "CLOSE"
                    | "DUPLICATE_FILL" | "DUPLICATE_EXIT" | "REJECTED_INVALID",
        "position_id": str | None,
        "qty_closed": int,
        "realized_pnl": float,
    }
"""
from typing import Dict, Any, Optional
from datetime import datetime, timedelta, timezone
import uuid
import logging
from pymongo.errors import DuplicateKeyError

from core.position_lifecycle import (
    normalize_strategy_risk,
    position_risk_prices,
)

logger = logging.getLogger("quantg.portfolio_ledger")


def _result(accepted: bool, action: str, position_id: Optional[str] = None,
            qty_closed: int = 0, realized_pnl: float = 0.0) -> Dict[str, Any]:
    return {
        "accepted": accepted,
        "action": action,
        "position_id": position_id,
        "qty_closed": qty_closed,
        "realized_pnl": realized_pnl,
    }


class PortfolioLedger:
    def __init__(self, db):
        self.db = db

    async def _record_trade_fill(self, fill: Dict[str, Any], position_id: str,
                                 action: str, realized_pnl: float = 0.0) -> None:
        """Immutable audit row for every fill the ledger ACCEPTED."""
        doc = {
            "id": f"tf_{uuid.uuid4().hex[:12]}",
            "fill_id": fill.get("id") or fill.get("fill_id"),
            "order_id": fill.get("order_id"),
            "position_id": position_id,
            "user_id": fill.get("user_id"),
            "strategy_id": fill.get("strategy_id"),
            "symbol": fill.get("symbol"),
            "target_symbol": fill.get("target_symbol"),
            "trading_symbol": fill.get("target_symbol"),
            "instrument_key": fill.get("instrument_key"),
            "side": fill.get("side"),
            "qty": int(fill.get("qty") or 0),
            "price": float(fill.get("price") or 0),
            "charges": float(fill.get("charges") or fill.get("brokerage") or 0.0),
            "brokerage": float(fill.get("brokerage") or 0.0),
            "trade_value": fill.get("trade_value"),
            "mode": fill.get("mode"),
            "action": action,
            "realized_pnl": round(realized_pnl, 2),
            "exit_reason": fill.get("exit_reason"),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self.db.trade_fills.insert_one(doc)
        except Exception as exc:  # audit row must never break accounting
            logger.error("Ledger: trade_fills insert failed for fill %s: %s",
                         doc.get("fill_id"), exc)

    async def _record_trade(self, pos: Dict[str, Any], fill: Dict[str, Any],
                            exit_qty: int, exit_price: float, gross_pnl: float,
                            net_pnl: float, entry_charges_part: float,
                            exit_charges: float, now_str: str) -> None:
        """Round-trip trade row written on every full close.

        db.trades is what win-rate / expectancy / daily-loss analytics read.
        """
        entry_time = pos.get("entry_time") or pos.get("created_at")
        duration_min = None
        try:
            entry_dt = datetime.fromisoformat(str(entry_time).replace("Z", "+00:00"))
            duration_min = round(
                (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60.0, 1
            )
        except Exception:
            pass
        avg_entry = float(pos.get("average_price") or pos.get("average_buy_price") or 0)
        doc = {
            "id": f"trade_{uuid.uuid4().hex[:12]}",
            "position_id": pos.get("id"),
            "user_id": pos.get("user_id"),
            "strategy_id": pos.get("strategy_id"),
            "symbol": pos.get("symbol"),
            "target_symbol": pos.get("target_symbol"),
            "trading_symbol": pos.get("target_symbol"),
            "instrument_key": pos.get("instrument_key"),
            "underlying": pos.get("underlying"),
            "asset_type": pos.get("asset_type"),
            "mode": pos.get("mode"),
            "position_side": pos.get("position_side"),
            "quantity": exit_qty,
            "qty": exit_qty,
            "entry_price": avg_entry,
            "exit_price": exit_price,
            "gross_pnl": round(gross_pnl, 2),
            "charges": round(entry_charges_part + exit_charges, 2),
            "entry_charges": round(entry_charges_part, 2),
            "exit_charges": round(exit_charges, 2),
            "net_pnl": round(net_pnl, 2),
            "pnl": round(net_pnl, 2),
            "realized_pnl": round(net_pnl, 2),
            "realised_pnl": round(net_pnl, 2),
            "is_win": net_pnl > 0,
            "exit_reason": fill.get("exit_reason") or "exit-fill",
            "entry_time": entry_time,
            "exit_time": now_str,
            "duration_minutes": duration_min,
            "exit_fill_id": fill.get("id") or fill.get("fill_id"),
            "created_at": now_str,
        }
        try:
            await self.db.trades.insert_one(doc)
        except Exception as exc:
            logger.error("Ledger: trades insert failed for position %s: %s",
                         pos.get("id"), exc)

    async def process_fill(self, fill: Dict[str, Any]) -> Dict[str, Any]:
        """Processes a trade fill, updating or closing corresponding strategy and portfolio positions.

        DB-level fill idempotency: inserts a record into processed_fill_ids with
        a unique index on fill_id. If the fill was already processed (duplicate key),
        this method returns immediately — making duplicate fills physically impossible
        regardless of how many async loops call this simultaneously.

        Returns a result dict (see module docstring). Callers MUST inspect
        result["accepted"] before crediting wallets or confirming exits.
        """
        # ── DB-level fill idempotency ──────────────────────────────────────────
        fill_id = fill.get("id") or fill.get("fill_id")
        if fill_id:
            try:
                await self.db.processed_fill_ids.insert_one({
                    "fill_id": fill_id,
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                })
            except DuplicateKeyError:
                logger.warning("Ledger: fill %s already processed — duplicate skipped", fill_id)
                return _result(False, "DUPLICATE_FILL")

        try:
            return await self._apply_fill(fill, fill_id)
        except Exception:
            # If the fill was NOT applied, release the idempotency marker so a
            # corrected retry isn't permanently blocked by a phantom "processed" flag.
            if fill_id:
                try:
                    await self.db.processed_fill_ids.delete_one({"fill_id": fill_id})
                except Exception:
                    pass
            raise

    async def _apply_fill(self, fill: Dict[str, Any], fill_id: Optional[str]) -> Dict[str, Any]:
        user_id = fill["user_id"]
        strategy_id = fill["strategy_id"]
        target_symbol = fill["target_symbol"]
        side = fill["side"]
        qty = int(fill["qty"])
        price = float(fill["price"])
        mode = fill["mode"]
        now_str = datetime.now(timezone.utc).isoformat()
        fill_charges = float(fill.get("charges") or fill.get("brokerage") or 0.0)
        stop_loss = fill.get("stop_loss")
        take_profit = fill.get("take_profit")
        protection = {
            "stoploss_price": stop_loss,
            "stop_loss": stop_loss,
            "target_price": take_profit,
            "take_profit": take_profit,
            "protection_status": "PROTECTED" if stop_loss or take_profit else "UNPROTECTED",
        }

        # ── Find the live position for this strategy+symbol+mode ──────────────
        # CRITICAL: include EXITING. The exit handler marks a position EXITING
        # *before* placing the exit order, so the exit fill arrives while the
        # position is EXITING — matching only OPEN made every legitimate exit
        # fill look like an orphan, which is exactly how positions got stuck
        # and wallets got over-credited by duplicate exits.
        pos = await self.db.strategy_positions.find_one({
            "user_id": user_id,
            "strategy_id": strategy_id,
            "target_symbol": target_symbol,
            "mode": mode,
            "status": {"$in": ["OPEN", "EXITING"]},
        })

        if not pos:
            # Guard: a SELL fill with no matching live position could be a duplicate
            # exit fill (the first exit already CLOSED the LONG). Creating a new
            # SHORT position from it would accumulate phantom short exposure.
            # NOTE: only CLOSED counts as "already exited" — EXITING positions are
            # matched by the main query above, so they never reach this branch.
            if side == "SELL":
                closed_long = await self.db.strategy_positions.find_one({
                    "user_id": user_id,
                    "strategy_id": strategy_id,
                    "target_symbol": target_symbol,
                    "mode": mode,
                    "position_side": "LONG",
                    "status": "CLOSED",
                })
                if closed_long:
                    logger.warning(
                        "Ledger: SELL fill %s for %s (strategy=%s) found no live LONG but a "
                        "CLOSED one exists — duplicate exit fill, REJECTED (no wallet credit).",
                        fill_id, target_symbol, strategy_id,
                    )
                    return _result(False, "DUPLICATE_EXIT")

            # Create a brand new position (no pre-existing open state)
            pos_id = f"pos_{uuid.uuid4().hex[:12]}"
            opt = fill.get("option_contract") or {}
            is_option = bool(opt) or fill.get("asset_type") == "option"

            # ── Hard validation for option positions ───────────────────────────
            # An option position missing instrument_key, lot_size, expiry, or
            # underlying CANNOT be exited later — _close_strategy_positions needs
            # all of these to reconstruct the exit order. Reject the fill now
            # rather than silently create an un-closeable ghost position.
            if is_option:
                _ikey_val = fill.get("instrument_key") or opt.get("instrument_key")
                _lot_val  = int(opt.get("lot_size") or fill.get("lot_size") or 0)
                _exp_val  = opt.get("expiry") or fill.get("expiry")
                _under    = opt.get("underlying") or fill.get("underlying") or fill.get("symbol")
                _otype    = opt.get("option_type") or fill.get("option_type")
                missing = []
                if not _ikey_val:
                    missing.append("instrument_key")
                if _lot_val <= 0:
                    missing.append("lot_size")
                if not _exp_val:
                    missing.append("expiry")
                if not _under:
                    missing.append("underlying")
                if not _otype:
                    missing.append("option_type")
                if missing:
                    logger.error(
                        "Ledger REJECTED option fill for %s (fill_id=%s): missing fields %s. "
                        "Position NOT created. Fix the order path that omits these fields.",
                        target_symbol, fill_id, missing,
                    )
                    raise ValueError(
                        f"Option position creation rejected: fill is missing {missing} "
                        f"for {target_symbol}. Cannot create an un-closeable position."
                    )

            position_doc = {
                "id": pos_id,
                "user_id": user_id,
                "strategy_id": strategy_id,
                "symbol": fill["symbol"],
                "target_symbol": target_symbol,
                "trading_symbol": target_symbol,
                "instrument_key": fill.get("instrument_key"),
                "instrument_token": fill.get("instrument_token"),
                "exchange": fill.get("exchange") or ("NFO" if is_option else "NSE"),
                "mode": mode,
                "quantity": qty,
                "open_quantity": qty,
                "qty": qty,
                "average_price": price,
                "average_buy_price": price,
                "avg_price": price,
                "entry_price": price,
                "position_side": "LONG" if side == "BUY" else "SHORT",
                "status": "OPEN",
                "realised_pnl": 0.0,
                "realized_pnl": 0.0,
                "unrealised_pnl": 0.0,
                "pnl": 0.0,
                "brokerage": float(fill.get("brokerage", 0.0)),
                "entry_charges": fill_charges,
                "created_at": now_str,
                "updated_at": now_str,
                "entry_time": now_str,
                "source_fill_id": fill["id"],
                "tp_sl_tsl_config": protection,
                # Option contract fields — required by _close_strategy_positions to build exit orders
                "asset_type": "option" if is_option else (fill.get("asset_type") or "equity"),
                "asset_class": fill.get("asset_class") or ("OPTION_LONG" if is_option and side == "BUY" else ("OPTION_SHORT" if is_option and side == "SELL" else None)),
                "product": fill.get("product") or opt.get("product") or "MIS",
                "lot_size": int(opt.get("lot_size") or fill.get("lot_size") or 1),
                "strike": opt.get("strike"),
                "expiry": opt.get("expiry"),
                "option_type": opt.get("option_type"),
                "underlying": opt.get("underlying") or fill.get("underlying") or fill["symbol"],
                "symbol_group": opt.get("underlying") or fill.get("underlying") or fill["symbol"],
            }

            # ── Stamp ABSOLUTE risk prices + exit deadline at entry ─────────────
            # The monitor must never depend on recomputing percentages later:
            # sl_price / tp_price / deadline_at are fixed facts of this entry.
            risk_cfg = normalize_strategy_risk(protection)
            risk_prices = position_risk_prices(
                {**position_doc, "tp_sl_tsl_config": risk_cfg}
            )
            if risk_prices.get("stop_loss") is not None:
                risk_cfg["stoploss_price"] = risk_prices["stop_loss"]
                risk_cfg["stop_loss"] = risk_prices["stop_loss"]
                position_doc["sl_price"] = risk_prices["stop_loss"]
            if risk_prices.get("take_profit") is not None:
                risk_cfg["target_price"] = risk_prices["take_profit"]
                risk_cfg["take_profit"] = risk_prices["take_profit"]
                position_doc["tp_price"] = risk_prices["take_profit"]
            risk_cfg["protection_status"] = (
                "PROTECTED"
                if (risk_prices.get("stop_loss") or risk_prices.get("take_profit"))
                else "UNPROTECTED"
            )
            time_exit_min = int(risk_cfg.get("time_exit_minutes") or 0)
            if time_exit_min > 0:
                position_doc["deadline_at"] = (
                    datetime.now(timezone.utc) + timedelta(minutes=time_exit_min)
                ).isoformat()
            position_doc["tp_sl_tsl_config"] = risk_cfg

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
                        "stop_loss": position_doc.get("sl_price", stop_loss),
                        "take_profit": position_doc.get("tp_price", take_profit),
                        "tp_sl_tsl_config": risk_cfg,
                        "updated_at": now_str
                    }
                },
                upsert=True
            )
            await self._record_trade_fill(fill, pos_id, "OPEN")
            logger.info(f"Portfolio ledger created NEW position {pos_id} for strategy {strategy_id} symbol {target_symbol}")
            return _result(True, "OPEN", pos_id)

        # ── Position exists: adding or exiting? ────────────────────────────────
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
                        "brokerage": float(fill.get("brokerage", 0.0)),
                        "entry_charges": fill_charges,
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
            await self._record_trade_fill(fill, pos["id"], "ADD")
            logger.info(f"Portfolio ledger averaged existing position {pos['id']} to qty {new_qty}")
            return _result(True, "ADD", pos["id"])

        # ── Opposite transaction: exiting or partially exiting ─────────────────
        open_qty_before = int(pos["open_quantity"])
        exit_qty = min(qty, open_qty_before)
        remaining_qty = open_qty_before - exit_qty

        # P&L calculation (gross)
        pos_avg_price = float(pos.get("average_price") or pos.get("average_buy_price") or pos.get("avg_price") or 0)
        if pos_side == "LONG":
            gross_pnl = (price - pos_avg_price) * exit_qty
        else:
            gross_pnl = (pos_avg_price - price) * exit_qty

        # Charges-aware net P&L: this fill's exit charges + the proportional
        # share of entry charges for the quantity being closed.
        entry_charges_total = float(pos.get("entry_charges") or pos.get("brokerage") or 0.0)
        entry_charges_part = (
            entry_charges_total * (exit_qty / open_qty_before) if open_qty_before > 0 else 0.0
        )
        net_pnl = gross_pnl - fill_charges - entry_charges_part

        update_fields = {
            "open_quantity": remaining_qty,
            "updated_at": now_str,
            "last_exit_price": price,
        }
        is_full_close = remaining_qty <= 0
        if is_full_close:
            update_fields["status"] = "CLOSED"
            update_fields["closed_at"] = now_str
            update_fields["exit_reason"] = fill.get("exit_reason") or pos.get("exit_reason") or "exit-fill"
            update_fields["unrealised_pnl"] = 0.0
            update_fields["unrealized_pnl"] = 0.0

        await self.db.strategy_positions.update_one(
            {"id": pos["id"]},
            {
                "$set": update_fields,
                "$inc": {
                    "realised_pnl": net_pnl,
                    "realized_pnl": net_pnl,
                    "pnl": net_pnl,
                    "brokerage": float(fill.get("brokerage", 0.0)),
                    "entry_charges": -entry_charges_part if not is_full_close else 0.0,
                }
            }
        )

        # Mirror standard portfolio position closure
        if is_full_close:
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

        # Audit rows: trade_fills always, trades on full close
        action = "CLOSE" if is_full_close else "REDUCE"
        await self._record_trade_fill(fill, pos["id"], action, realized_pnl=net_pnl)
        if is_full_close:
            await self._record_trade(
                pos, fill, exit_qty, price, gross_pnl, net_pnl,
                entry_charges_part, fill_charges, now_str,
            )

        # Strategy scorecard: P&L always; trade/win/loss counters on full close
        scorecard_update: Dict[str, Any] = {
            "$set": {"last_pnl": net_pnl},
            "$inc": {"today_pnl": net_pnl, "total_pnl": net_pnl},
        }
        if is_full_close:
            scorecard_update["$inc"]["total_trades"] = 1
            if net_pnl > 0:
                scorecard_update["$inc"]["wins"] = 1
            elif net_pnl < 0:
                scorecard_update["$inc"]["losses"] = 1
        await self.db.strategies.update_one(
            {"id": strategy_id, "user_id": user_id},
            scorecard_update,
        )
        logger.info(
            "Portfolio ledger %s position %s. qty_closed=%s realized P&L: %.2f",
            "closed" if is_full_close else "reduced", pos["id"], exit_qty, net_pnl,
        )
        return _result(True, action, pos["id"], qty_closed=exit_qty, realized_pnl=net_pnl)
