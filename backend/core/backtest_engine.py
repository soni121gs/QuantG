"""QuantG Backtesting Engine — UNDERLYING / EQUITY backtester.

USE THIS FOR: equity / futures strategies, where the traded price IS the candle
price. DO NOT use for options — it prices trades on the underlying candle and so
ignores premium, theta and spread cost, returning a confidently-wrong grade for
any option strategy. For options use the OOS bhavcopy backtester
(core/eod_options_backtest.py). Dispatch is enforced in
routes/core_status.py:run_core_backtest by visual_config.options.enabled.

Runs AST sandboxed python strategy code sequentially over historical candle series.
Simulates realistic order fills at next candle open, enforces SL/TP risk boundaries,
enforces zero lookahead bias, and produces a complete mathematical scorecard.
"""
from typing import List, Dict, Any, Optional
import uuid
import logging
from datetime import datetime, timezone

from core.strategy_signal_service import StrategySignalService, CoreSignalType

logger = logging.getLogger("quantg.backtest_engine")


def _assess_data_quality(candles: List[Dict[str, Any]], total_trades: int) -> str:
    """Grade the input series, not just its length. A long but flat/degenerate
    series (no intrabar range) or one that produced too few trades to be
    statistically meaningful is NOT 'HIGH', however many bars it has.
    """
    n = len(candles)
    if n < 60:  # below the 50-bar warmup + a handful of evaluable bars
        return "INSUFFICIENT"
    # Fraction of bars carrying real intrabar range (high > low). A synthetic or
    # flat-spot series collapses to ~0 here and must not score HIGH.
    ranged = sum(1 for c in candles if float(c.get("high") or 0) > float(c.get("low") or 0))
    range_frac = ranged / n if n else 0.0
    if range_frac < 0.5:
        return "LOW"
    if total_trades < 10:
        return "LOW"        # too few trades for the scorecard to mean anything
    if n >= 500 and total_trades >= 30:
        return "HIGH"
    return "MEDIUM"


class BacktestEngine:
    def __init__(self, db):
        self.db = db

    async def run_backtest(
        self,
        strategy_id: str,
        python_code: str,
        candles: List[Dict[str, Any]],
        strategy_metadata: Dict[str, Any],
        slippage_pct: float = 0.0003,
        brokerage_per_trade: float = 20.0,
        stop_loss_pct: Optional[float] = None,
        take_profit_pct: Optional[float] = None
    ) -> Dict[str, Any]:
        """Runs a sequential backtest iteration.
        
        Simulates: Entry on next bar OPEN after signal triggers.
        """
        logger.info(f"Initiating backtest engine for strategy {strategy_id} over {len(candles)} bars.")
        
        trades: List[Dict[str, Any]] = []
        active_trade: Optional[Dict[str, Any]] = None
        
        equity_curve: List[float] = [100000.0]
        current_equity = 100000.0
        max_equity = 100000.0
        max_drawdown = 0.0
        
        # Determine lot sizes
        vc = strategy_metadata.get("visual_config") or {}
        opt_cfg = vc.get("options") or {}
        underlying = opt_cfg.get("underlying") or strategy_metadata.get("symbol") or "NIFTY"
        
        # Resolve via market_domains (source of truth) — falls back to 1 for
        # cash equity, which is correct for this underlying/equity engine.
        from core.market_domains import resolve_domain_by_underlying
        lot_size = resolve_domain_by_underlying(underlying).get_lot_size(underlying)

        qty = lot_size * int(opt_cfg.get("lots") or 1)

        # Sequential bar-by-bar evaluation (zero lookahead)
        for i in range(50, len(candles) - 1):
            history = candles[:i+1]
            next_candle = candles[i+1]
            
            # 1. Check if stoploss or target is hit on current bar
            if active_trade:
                high_price = float(candles[i]["high"])
                low_price = float(candles[i]["low"])
                close_price = float(candles[i]["close"])
                
                # Check Stop Loss
                sl_hit = False
                sl_price = active_trade.get("stop_loss")
                if sl_price:
                    if active_trade["side"] == "BUY" and low_price <= sl_price:
                        sl_hit = True
                    elif active_trade["side"] == "SELL" and high_price >= sl_price:
                        sl_hit = True
                        
                # Check Take Profit
                tp_hit = False
                tp_price = active_trade.get("take_profit")
                if tp_price:
                    if active_trade["side"] == "BUY" and high_price >= tp_price:
                        tp_hit = True
                    elif active_trade["side"] == "SELL" and low_price <= tp_price:
                        tp_hit = True

                if sl_hit or tp_hit:
                    # Close trade at target or SL
                    exit_price = sl_price if sl_hit else tp_price
                    pnl = (exit_price - active_trade["entry_price"]) * qty if active_trade["side"] == "BUY" else (active_trade["entry_price"] - exit_price) * qty
                    pnl -= (brokerage_per_trade * 2) # round trip
                    
                    active_trade["exit_price"] = exit_price
                    active_trade["exit_date"] = candles[i]["date"]
                    active_trade["pnl"] = round(pnl, 2)
                    active_trade["exit_reason"] = "STOP_LOSS" if sl_hit else "TAKE_PROFIT"
                    
                    trades.append(active_trade)
                    current_equity += pnl
                    active_trade = None
                    continue

            # 2. Evaluate strategy rules on historical block
            signal, meta = StrategySignalService.evaluate_strategy(
                python_code, history, strategy_metadata
            )
            
            # 3. Process Signals
            if signal == CoreSignalType.EXIT and active_trade:
                # Close trade at NEXT bar open
                exit_price = float(next_candle["open"])
                pnl = (exit_price - active_trade["entry_price"]) * qty if active_trade["side"] == "BUY" else (active_trade["entry_price"] - exit_price) * qty
                pnl -= (brokerage_per_trade * 2)
                
                active_trade["exit_price"] = exit_price
                active_trade["exit_date"] = next_candle["date"]
                active_trade["pnl"] = round(pnl, 2)
                active_trade["exit_reason"] = "STRATEGY_EXIT"
                
                trades.append(active_trade)
                current_equity += pnl
                active_trade = None
                
            elif signal in (CoreSignalType.BUY_CALL, CoreSignalType.BUY_PUT) and not active_trade:
                # Open trade at NEXT bar open
                entry_price = float(next_candle["open"])
                slippage = entry_price * slippage_pct
                final_entry_price = round(entry_price + slippage if signal == CoreSignalType.BUY_CALL else entry_price - slippage, 2)
                
                # Define boundaries
                sl_price = None
                tp_price = None
                if stop_loss_pct:
                    sl_price = final_entry_price * (1 - stop_loss_pct) if signal == CoreSignalType.BUY_CALL else final_entry_price * (1 + stop_loss_pct)
                if take_profit_pct:
                    tp_price = final_entry_price * (1 + take_profit_pct) if signal == CoreSignalType.BUY_CALL else final_entry_price * (1 - take_profit_pct)
                
                active_trade = {
                    "id": f"bt_trade_{uuid.uuid4().hex[:8]}",
                    "side": "BUY" if signal == CoreSignalType.BUY_CALL else "SELL",
                    "entry_price": final_entry_price,
                    "entry_date": next_candle["date"],
                    "qty": qty,
                    "stop_loss": sl_price,
                    "take_profit": tp_price
                }
                
            # Drawdown calculations
            equity_curve.append(current_equity)
            if current_equity > max_equity:
                max_equity = current_equity
            dd = (max_equity - current_equity) / max_equity
            if dd > max_drawdown:
                max_drawdown = dd

        # Final cleanup for hanging positions
        if active_trade:
            exit_price = float(candles[-1]["close"])
            pnl = (exit_price - active_trade["entry_price"]) * qty if active_trade["side"] == "BUY" else (active_trade["entry_price"] - exit_price) * qty
            active_trade["exit_price"] = exit_price
            active_trade["exit_date"] = candles[-1]["date"]
            active_trade["pnl"] = round(pnl, 2)
            active_trade["exit_reason"] = "FORCE_CLOSE_END"
            trades.append(active_trade)
            current_equity += pnl

        # Calculate Scorecard Metrics
        total_pnl = round(current_equity - 100000.0, 2)
        total_trades = len(trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        win_rate = round(len(wins) / total_trades, 2) if total_trades > 0 else 0.0
        
        gross_profits = sum(t["pnl"] for t in wins)
        gross_losses = abs(sum(t["pnl"] for t in losses))
        profit_factor = round(gross_profits / gross_losses, 2) if gross_losses > 0 else 99.0
        
        avg_win = round(gross_profits / len(wins), 2) if len(wins) > 0 else 0.0
        avg_loss = round(gross_losses / len(losses), 2) if len(losses) > 0 else 0.0
        
        # Risk-adjusted return: Sharpe & Sortino from per-trade returns (as a
        # fraction of starting capital). These are the honest objective-function
        # inputs an AutoResearch loop must optimise — a high win_rate with a fat
        # tail scores poorly here, unlike the naive strategy_score below.
        sharpe = 0.0
        sortino = 0.0
        if total_trades >= 2:
            rets = [t["pnl"] / 100000.0 for t in trades]
            n = len(rets)
            mean_r = sum(rets) / n
            var = sum((r - mean_r) ** 2 for r in rets) / (n - 1)
            std = var ** 0.5
            # Annualise by trade frequency (trades observed over the test window
            # scaled to a ~252-session year); falls back to per-trade Sharpe.
            ann = (252.0 / n) ** 0.5 if n > 0 else 1.0
            sharpe = round((mean_r / std) * ann, 3) if std > 0 else 0.0
            downside = [r for r in rets if r < 0]
            if downside:
                dvar = sum(r ** 2 for r in downside) / len(downside)
                dstd = dvar ** 0.5
                sortino = round((mean_r / dstd) * ann, 3) if dstd > 0 else 0.0
            elif mean_r > 0:
                sortino = 99.0

        # Score calculation
        strategy_score = round((win_rate * 50) + (profit_factor * 10) - (max_drawdown * 100), 2)

        result_doc = {
            "id": f"run_{uuid.uuid4().hex[:12]}",
            "strategy_id": strategy_id,
            "underlying": underlying,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "drawdown": round(max_drawdown, 4),
            "max_drawdown_pct": round(max_drawdown * 100, 2),
            "total_trades": total_trades,
            "pnl": total_pnl,
            "return_pct": round((total_pnl / 100000.0) * 100, 2),
            "average_win": avg_win,
            "average_loss": avg_loss,
            "sharpe": sharpe,
            "sortino": sortino,
            "strategy_score": strategy_score,
            "data_quality": _assess_data_quality(candles, total_trades),
            "trades": trades,
            "equity_curve": equity_curve,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        
        # Save to Mongo
        await self.db.backtest_runs.insert_one(result_doc)
        logger.info(f"Backtest completed successfully. Return: {result_doc['return_pct']}%, Win Rate: {win_rate * 100}%")
        
        return result_doc
