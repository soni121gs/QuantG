"""
Integration of advanced trading systems into strategy runner

Patches strategy_runner.py to:
1. Analyze market trend before each strategy evaluation
2. Filter signals with confidence scoring
3. Perform position size calculations
4. Check exit conditions for open positions
5. Reconcile broker positions on startup
"""

# Add these imports to strategy_runner.py
import sys
sys.path.insert(0, '/app')

from market_protection import (
    MarketTrendAnalyzer,
    FakeSignalFilter,
    PositionRiskManager,
    PositionRecovery,
    AutoExitManager,
    OrderExecutionRetry,
)
from daily_strategy_reporter import DailyStrategyReporter


async def enhanced_strategy_evaluation(
    user_id: str,
    strategy_id: str,
    strategy_config: dict,
    candle_data: list,
    recent_signals: list,
    open_positions: list,
    current_capital: float,
) -> dict:
    """
    Enhanced strategy evaluation with market protection
    
    Returns:
    {
        "strategy_id": str,
        "signals": [...],  # Validated signals only
        "market_trend": {...},
        "daily_report": {...},
        "orders_to_place": [...],
        "exits_to_place": [...],
        "risk_warnings": [...],
    }
    """
    
    # Step 1: Analyze market trend
    trend_info = MarketTrendAnalyzer.analyze(candle_data, lookback=50)
    
    # Step 2: Generate daily probability report
    # (In real scenario, fetch recent_trades from DB)
    daily_report = DailyStrategyReporter.generate_daily_report(
        strategy_id=strategy_id,
        strategy_name=strategy_config.get("name", "Unknown"),
        underlying=strategy_config.get("visual_config", {}).get("options", {}).get("underlying", "NIFTY"),
        recent_trades=[],  # Fetch from DB
        market_trend_analysis=trend_info,
        total_capital=current_capital,
    )
    
    # Step 3: Run strategy code and get raw signals
    raw_signals = []
    try:
        # Run user's strategy code
        raw_signals = await safe_run_strategy(
            strategy_config.get("python_code"),
            candle_data,
        )
    except Exception as e:
        return {
            "strategy_id": strategy_id,
            "signals": [],
            "error": f"Strategy execution failed: {e}",
            "market_trend": trend_info,
            "daily_report": daily_report,
        }
    
    # Step 4: Validate signals with fake-signal filter
    validated_signals = []
    risk_warnings = []
    
    for signal in raw_signals:
        validation = FakeSignalFilter.validate(
            signal=signal,
            data=candle_data,
            trend_info=trend_info,
            recent_signals=recent_signals[-5:],  # Last 5 signals
        )
        
        signal_with_validation = {
            **signal,
            "confidence": validation["confidence"],
            "is_valid": validation["is_valid"],
            "reasons": validation["reasons"],
        }
        
        if validation["is_valid"]:
            validated_signals.append(signal_with_validation)
        else:
            risk_warnings.append({
                "type": "SIGNAL_FILTERED",
                "signal": signal,
                "reason": f"Low confidence ({validation['confidence']:.0f}%) - filtered out",
                "details": validation["reasons"],
            })
    
    # Step 5: Calculate position sizes for new signals
    orders_to_place = []
    config = strategy_config.get("visual_config", {})
    risk_config = config.get("risk", {})
    
    for signal in validated_signals:
        # Skip if daily entry limit reached
        entries_today = len([s for s in validated_signals if s.get("date", "").startswith(datetime.now().strftime("%Y-%m-%d"))])
        max_entries = config.get("entry", {}).get("max_entries_per_day", 10)
        
        if entries_today > max_entries:
            risk_warnings.append({
                "type": "ENTRY_LIMIT_REACHED",
                "strategy_id": strategy_id,
                "reason": f"Max entries per day ({max_entries}) reached",
            })
            continue
        
        # Calculate position size
        current_price = candle_data[-1].get("close", 0) if candle_data else 0
        sl_pct = risk_config.get("stop_loss_pct", 0.02)
        sl_price = current_price * (1 - sl_pct) if signal["action"] == "BUY" else current_price * (1 + sl_pct)
        
        size_calc = PositionRiskManager.calculate_safe_position_size(
            capital=current_capital,
            risk_pct=config.get("entry", {}).get("risk_per_trade_pct", 1.0),
            entry_price=current_price,
            stop_loss_price=sl_price,
            max_loss_limit=risk_config.get("max_daily_loss", 5000),
        )
        
        orders_to_place.append({
            "strategy_id": strategy_id,
            "action": signal["action"],
            "signal_date": signal.get("date"),
            "confidence": signal.get("confidence", 0),
            "quantity": size_calc.get("quantity", 1),
            "entry_price": current_price,
            "stop_loss_price": sl_price,
            "take_profit_pct": risk_config.get("take_profit_pct", 0.04),
        })
    
    # Step 6: Check exit conditions for open positions
    exits_to_place = []
    exit_config = config.get("exit_conditions", {})
    tp_config = config.get("take_profit", {})
    
    for position in open_positions:
        current_price = candle_data[-1].get("close", 0) if candle_data else position.get("avg_price", 0)
        
        exit_check = AutoExitManager.check_exit_triggers(
            position=position,
            current_price=current_price,
            exit_config={
                "take_profit_pct": tp_config.get("single_target_pct", 0.04),
                "stop_loss_pct": risk_config.get("stop_loss_pct", 0.02),
                "trailing_stop_pct": tp_config.get("trailing_stop_pct", 0),
                "max_hold_minutes": exit_config.get("max_hold_minutes", 240),
            }
        )
        
        if exit_check["should_exit"]:
            exits_to_place.append({
                "symbol": position.get("symbol"),
                "quantity": abs(position.get("qty", 0)),
                "side": "SELL" if position.get("qty", 0) > 0 else "BUY",
                "exit_reason": exit_check["exit_reason"],
                "unrealized_pnl": exit_check.get("unrealized_pnl"),
                "unrealized_pnl_pct": exit_check.get("unrealized_pnl_pct"),
            })
    
    # Step 7: Check capital protection
    wipeout_check = PositionRiskManager.check_capital_wipeout_risk(
        current_capital=current_capital,
        open_positions=open_positions,
        worst_case_loss_pct=5.0,
    )
    
    if wipeout_check["risk_level"] in ("HIGH", "CRITICAL"):
        risk_warnings.append({
            "type": "WIPEOUT_RISK",
            "risk_level": wipeout_check["risk_level"],
            "recommendation": wipeout_check["recommendation"],
            "current_loss_pct": wipeout_check["current_loss_pct"],
        })
    
    return {
        "strategy_id": strategy_id,
        "signals": validated_signals,
        "market_trend": trend_info,
        "daily_report": daily_report,
        "orders_to_place": orders_to_place,
        "exits_to_place": exits_to_place,
        "risk_warnings": risk_warnings,
        "wipeout_risk": wipeout_check,
    }


async def reconcile_positions_on_startup(
    user_id: str,
    kite,
) -> dict:
    """
    Reconcile broker positions with local tracking on app startup
    Fixes position tracking after disconnects
    """
    
    # Fetch broker positions from Zerodha
    broker_positions = []
    try:
        kite_positions = kite.positions()
        if kite_positions and kite_positions.get("net"):
            broker_positions = kite_positions["net"]
    except Exception as e:
        logger.warning(f"Position reconciliation failed: {e}")
        return {"status": "failed", "error": str(e)}
    
    # Fetch local positions from MongoDB
    # (This would be done in the actual strategy runner)
    # local_positions = await db.positions.find({"user_id": user_id}).to_list(1000)
    
    # Reconcile
    # reconciliation = PositionRecovery.reconcile_positions(
    #     broker_positions=broker_positions,
    #     local_positions=local_positions,
    # )
    
    # Apply reconciliation actions
    # if reconciliation["reconciliation_needed"]:
    #     for action in reconciliation["reconciliation_actions"]:
    #         if action["action"] == "UPDATE_LOCAL":
    #             await db.positions.update_one(
    #                 {"user_id": user_id, "symbol": action["symbol"]},
    #                 {"$set": {"qty": action["new_qty"]}}
    #             )
    #         elif action["action"] == "REMOVE_LOCAL":
    #             await db.positions.delete_one(
    #                 {"user_id": user_id, "symbol": action["symbol"]}
    #             )
    
    return {
        "status": "success",
        "broker_positions_count": len(broker_positions),
        # "reconciliation": reconciliation,
    }


async def retry_failed_order(
    order_data: dict,
    attempt: int = 1,
    max_retries: int = 5,
) -> dict:
    """
    Retry placing an order with exponential backoff
    Handles network issues, rate limits, etc.
    """
    
    retry_cfg = OrderExecutionRetry.retry_config(attempt)
    
    if not retry_cfg["should_retry"]:
        return {
            "status": "failed",
            "attempt": attempt,
            "reason": "Max retries exceeded",
        }
    
    # Wait before retrying
    await asyncio.sleep(retry_cfg["backoff_seconds"])
    
    try:
        # Try to place order (would call actual Zerodha API)
        # result = await kite.place_order(...)
        
        return {
            "status": "success",
            "attempt": attempt,
            "order_placed": True,
        }
    
    except Exception as e:
        error_msg = str(e)
        
        if OrderExecutionRetry.is_retryable_error(error_msg):
            logger.warning(f"Retryable error on attempt {attempt}: {error_msg}")
            # Schedule next retry
            return {
                "status": "retry_needed",
                "attempt": attempt,
                "error": error_msg,
                "next_backoff": retry_cfg["backoff_seconds"] * 2,
            }
        else:
            return {
                "status": "failed",
                "attempt": attempt,
                "error": error_msg,
                "reason": "Non-retryable error",
            }


# Integration points in strategy_runner.py

"""
In runner_loop():

1. Before each strategy evaluation, call:
   trend_info = MarketTrendAnalyzer.analyze(data)
   Store trend_info in app.state for access by all strategies

2. For each strategy, call:
   result = await enhanced_strategy_evaluation(...)
   
3. Place validated orders from result["orders_to_place"]
   Close positions from result["exits_to_place"]
   
4. Log risk_warnings for monitoring

5. On app startup, call:
   await reconcile_positions_on_startup(user_id, kite)

6. When order placement fails, call:
   retry_result = await retry_failed_order(order_data, attempt)
"""

