"""
Advanced Strategy Configuration Schema

Extends strategy visual_config with:
- Lot size and position sizing controls
- Advanced stop loss (fixed %, ATR-based, breakeven)
- Advanced take profit (scaling targets, time-based)
- Automatic exit conditions (time, reversal, volatility)
- Risk management (max daily loss, correlation hedging)
- Order management (retry logic, partial fills, slippage tolerance)
"""

from typing import Dict, Any, List

ADVANCED_STRATEGY_CONFIG_SCHEMA = {
    "version": "2.0",
    "position_sizing": {
        "mode": "FIXED_LOTS",  # FIXED_LOTS | KELLY | VOLATILITY_ADJUSTED
        "fixed_lots": 1,  # For FIXED_LOTS mode
        "kelly_fraction": 0.25,  # Kelly % (0.25 = quarter Kelly)
        "max_position_notional": 100000,  # Max ₹ exposure per trade
        "scale_with_volatility": False,
        "volatility_lookback": 20,
    },
    
    "entry": {
        "signal_confidence_minimum": 40,  # Only take signals >= 40% confidence
        "trend_alignment_required": True,  # Must align with current trend?
        "max_entries_per_day": 10,  # Prevent over-trading
        "entry_limit_price": None,  # LIMIT order if set; MARKET if None
        "entry_slippage_tolerance_pct": 0.5,  # Accept up to 0.5% worse fill
    },
    
    "stop_loss": {
        "type": "PERCENT",  # PERCENT | ATR | BREAKEVEN | CHANDELIER
        "percent_value": 2.0,  # 2% stop loss
        "atr_multiplier": 2.0,  # 2 × ATR
        "atr_period": 14,
        "chandelier_lookback": 22,  # Chandelier ATR stop
        "breakeven_cushion_pct": 0.5,  # Move SL to breakeven + 0.5%
        "breakeven_trigger_pct": 1.5,  # After 1.5% profit, enable breakeven
        "hard_stop_enabled": True,
        "hard_stop_limit_pct": 5.0,  # Absolute max loss per trade
    },
    
    "take_profit": {
        "type": "SINGLE",  # SINGLE | SCALE_OUT | TIME_BASED
        "single_target_pct": 4.0,  # Exit 100% at 4% profit
        
        # Scale-out config: exit % at each target
        "scale_targets": [
            {"pct_profit": 2.0, "qty_pct": 0.25},  # Exit 25% at 2% profit
            {"pct_profit": 3.5, "qty_pct": 0.35},  # Exit 35% at 3.5% profit
            {"pct_profit": 5.0, "qty_pct": 0.40},  # Exit remaining 40% at 5% profit
        ],
        
        # Time-based: hold until expiry time
        "hold_minutes": 30,  # Hold for 30 minutes, then exit at market
        "exit_on_market_close": False,  # Force exit at 3:25 PM IST?
    },
    
    "trailing_stop": {
        "enabled": False,
        "activation_profit_pct": 2.0,  # Activate after 2% profit
        "trail_pct": 1.0,  # Trail by 1%
        "trail_in_pips": None,  # Or trail by fixed pips (NIFTY = 100 pips ≈ 10 points)
    },
    
    "exit_conditions": {
        "max_hold_minutes": 240,  # Force exit after 4 hours
        "exit_on_reverse_signal": True,  # Exit if opposite signal appears
        "exit_on_reversal_candle": True,  # Exit if engulfing candle forms
        "exit_on_time_of_day": None,  # Exit at specific time (e.g., "14:30")
        "exit_on_volatility_spike": True,
        "volatility_threshold_atr_multiplier": 3.0,  # Exit if ATR > 3× normal
    },
    
    "correlation_hedging": {
        "enabled": False,
        "hedge_when_corr_exceeds": 0.8,  # If correlated instruments move against
        "hedge_instruments": ["BANKNIFTY"],  # Hedge with these
        "hedge_qty_ratio": 0.5,  # Hedge 50% of main position
    },
    
    "order_management": {
        "order_type": "MARKET",  # MARKET | LIMIT | SMART (convert LIMIT to MARKET after delay)
        "partial_fill_handling": "ACCEPT",  # ACCEPT | CANCEL_REST | RETRY_FULL
        "partial_fill_wait_seconds": 5,
        
        "retry_on_failure": True,
        "retry_max_attempts": 5,
        "retry_backoff_seconds": 2,  # Initial backoff; increases exponentially
        
        "slippage_tolerance_pct": 0.5,  # Cancel order if slippage exceeds
    },
    
    "risk_limits": {
        "max_risk_per_trade_pct": 1.0,  # Risk max 1% of capital per trade
        "max_daily_loss_amount": 5000,  # Max ₹5000 loss per day
        "max_concurrent_positions": 3,  # Never more than 3 open trades
        "max_correlation_risk": 0.7,  # Positions can't all be highly correlated
        "circuit_breaker_daily_loss_pct": 5.0,  # Pause trading if daily loss > 5%
    },
    
    "advanced": {
        "use_smart_orders": False,  # Use Zerodha's bracket/cover orders
        "bracket_order_mode": False,  # Place SL+TP together (if broker supports)
        "gtt_enabled": False,  # Good-Till-Triggered orders for exits
        "oco_enabled": False,  # One-Cancels-Other (SL or TP)
        "algo_order": False,  # Use TWAP/VWAP for large positions
    },
}

# Preset configurations for common trading styles
STRATEGY_PRESETS = {
    "AGGRESSIVE": {
        "position_sizing": {"mode": "VOLATILITY_ADJUSTED", "fixed_lots": 2},
        "stop_loss": {"percent_value": 1.5, "hard_stop_limit_pct": 3.0},
        "take_profit": {"single_target_pct": 3.0},
        "risk_limits": {"max_daily_loss_amount": 10000, "max_concurrent_positions": 5},
    },
    
    "BALANCED": {
        "position_sizing": {"mode": "FIXED_LOTS", "fixed_lots": 1},
        "stop_loss": {"percent_value": 2.0, "hard_stop_limit_pct": 4.0},
        "take_profit": {"type": "SCALE", "single_target_pct": 3.0},
        "trailing_stop": {"enabled": True, "activation_profit_pct": 1.5},
        "risk_limits": {"max_daily_loss_amount": 5000, "max_concurrent_positions": 3},
    },
    
    "CONSERVATIVE": {
        "position_sizing": {"mode": "FIXED_LOTS", "fixed_lots": 1},
        "entry": {"signal_confidence_minimum": 50},
        "stop_loss": {"percent_value": 3.0, "hard_stop_limit_pct": 5.0},
        "take_profit": {"single_target_pct": 2.5},
        "exit_conditions": {"max_hold_minutes": 60},
        "risk_limits": {"max_daily_loss_amount": 2500, "max_concurrent_positions": 2},
    },
    
    "SCALP": {
        "position_sizing": {"mode": "FIXED_LOTS", "fixed_lots": 1},
        "stop_loss": {"percent_value": 0.5, "hard_stop_limit_pct": 1.0},
        "take_profit": {"single_target_pct": 0.75},
        "exit_conditions": {"max_hold_minutes": 15},
        "order_management": {"order_type": "SMART"},
        "risk_limits": {"max_daily_loss_amount": 1000},
    },
    
    "SWING": {
        "position_sizing": {"mode": "KELLY", "kelly_fraction": 0.25},
        "stop_loss": {"type": "ATR", "atr_multiplier": 2.5},
        "take_profit": {"single_target_pct": 6.0},
        "trailing_stop": {"enabled": True, "activation_profit_pct": 2.0},
        "exit_conditions": {"max_hold_minutes": 480},  # Hold longer
        "risk_limits": {"max_daily_loss_amount": 10000, "max_concurrent_positions": 2},
    },
}

