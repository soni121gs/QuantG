"""
Advanced Market Protection & Signal Validation System

Features:
1. Trend detection (Bullish/Bearish/Neutral)
2. Fake signal filtering with ML-style confidence scoring
3. Daily win probability analyzer
4. Position risk management with capital protection
5. Order execution retry logic with exponential backoff
6. Position recovery on disconnects
"""

import math
from typing import Dict, List, Any, Tuple
from datetime import datetime, timedelta


class MarketTrendAnalyzer:
    """Detect market trend: Bullish, Bearish, or Neutral"""
    
    @staticmethod
    def analyze(data: List[Dict[str, Any]], lookback: int = 50) -> Dict[str, Any]:
        """
        Analyze trend from candle data
        Returns: {trend, strength (0-100), direction, support, resistance, reversal_risk}
        """
        if len(data) < lookback:
            return {
                "trend": "NEUTRAL",
                "strength": 0,
                "direction": 0,
                "support": 0,
                "resistance": 0,
                "reversal_risk": 0.5,
                "error": "Insufficient data",
            }
        
        closes = [d['close'] for d in data[-lookback:]]
        highs = [d['high'] for d in data[-lookback:]]
        lows = [d['low'] for d in data[-lookback:]]
        
        # EMA for trend
        ema_fast = MarketTrendAnalyzer._ema(closes, 8)
        ema_slow = MarketTrendAnalyzer._ema(closes, 21)
        
        # Support/Resistance
        support = min(lows[-20:])
        resistance = max(highs[-20:])
        
        # RSI for overbought/oversold
        rsi = MarketTrendAnalyzer._rsi(closes, 14)
        
        # Trend strength (momentum)
        atr = MarketTrendAnalyzer._atr(highs, lows, closes, 14)
        current_price = closes[-1]
        prev_price = closes[-5] if len(closes) > 5 else closes[0]
        price_change = ((current_price - prev_price) / prev_price) * 100 if prev_price else 0
        
        # Determine trend
        trend = "NEUTRAL"
        strength = 0
        direction = 0
        reversal_risk = 0.5
        
        if ema_fast[-1] > ema_slow[-1]:
            trend = "BULLISH"
            strength = min(100, abs(price_change) * 2)
            direction = 1
            reversal_risk = min(1.0, (100 - rsi) / 100) if rsi else 0.5  # High RSI = reversal risk
        elif ema_fast[-1] < ema_slow[-1]:
            trend = "BEARISH"
            strength = min(100, abs(price_change) * 2)
            direction = -1
            reversal_risk = min(1.0, rsi / 100) if rsi else 0.5  # Low RSI = reversal risk
        else:
            trend = "NEUTRAL"
            strength = 20
            direction = 0
            reversal_risk = 0.7
        
        return {
            "trend": trend,
            "strength": round(strength, 2),
            "direction": direction,
            "support": round(support, 2),
            "resistance": round(resistance, 2),
            "rsi": round(rsi, 2),
            "atr": round(atr, 2),
            "reversal_risk": round(reversal_risk, 2),  # 0=low risk, 1=high risk
            "current_price": round(current_price, 2),
            "price_change_pct": round(price_change, 2),
        }
    
    @staticmethod
    def _ema(data: List[float], period: int) -> List[float]:
        ema = []
        multiplier = 2 / (period + 1)
        for i in range(len(data)):
            if i == 0:
                ema.append(data[i])
            else:
                ema.append(data[i] * multiplier + ema[i-1] * (1 - multiplier))
        return ema
    
    @staticmethod
    def _rsi(data: List[float], period: int = 14) -> float:
        if len(data) < period + 1:
            return 50.0
        gains = sum(max(data[i] - data[i-1], 0) for i in range(-period, 0))
        losses = sum(max(data[i-1] - data[i], 0) for i in range(-period, 0))
        avg_gain = gains / period
        avg_loss = losses / period if losses else 0.0001
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
    
    @staticmethod
    def _atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        if len(highs) < period:
            return 0.0
        trs = []
        for i in range(len(highs)):
            if i == 0:
                tr = highs[i] - lows[i]
            else:
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1]),
                )
            trs.append(tr)
        return sum(trs[-period:]) / period if trs else 0.0


class FakeSignalFilter:
    """Validate signals - reduce false/whipsaw trades"""
    
    @staticmethod
    def validate(
        signal: Dict[str, Any],
        data: List[Dict[str, Any]],
        trend_info: Dict[str, Any],
        recent_signals: List[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Validate a signal and assign confidence score
        Returns: {is_valid, confidence (0-100), reasons, filtered}
        """
        if recent_signals is None:
            recent_signals = []
        
        confidence = 50.0  # Start at neutral
        reasons = []
        
        action = signal.get("action", "").upper()
        if action not in ("BUY", "SELL"):
            return {"is_valid": False, "confidence": 0, "reasons": ["Invalid action"], "filtered": True}
        
        # Check 1: Align with trend
        trend = trend_info.get("trend", "NEUTRAL")
        reversal_risk = trend_info.get("reversal_risk", 0.5)
        
        if action == "BUY":
            if trend == "BULLISH":
                confidence += 25
                reasons.append("Signal aligned with bullish trend")
            elif trend == "BEARISH":
                confidence -= 30
                reasons.append("BUY signal against bearish trend (reversal attempt)")
            else:
                confidence += 5
                reasons.append("Signal in neutral trend")
        else:  # SELL
            if trend == "BEARISH":
                confidence += 25
                reasons.append("Signal aligned with bearish trend")
            elif trend == "BULLISH":
                confidence -= 30
                reasons.append("SELL signal against bullish trend (reversal attempt)")
            else:
                confidence += 5
                reasons.append("Signal in neutral trend")
        
        # Check 2: Reversal risk - if trend is risky, lower confidence for opposite-side signals
        if reversal_risk > 0.7:
            if (action == "BUY" and trend == "BEARISH") or (action == "SELL" and trend == "BULLISH"):
                confidence -= 20
                reasons.append(f"High reversal risk ({reversal_risk*100:.0f}%)")
        
        # Check 3: Recent whipsaw detection
        if len(recent_signals) > 0:
            last_signal = recent_signals[-1]
            if last_signal.get("action") != action:
                # Opposite direction - check if it's too recent
                bars_since = (len(data) - data.index(signal)) if signal in data else 5
                if bars_since < 3:
                    confidence -= 25
                    reasons.append(f"Whipsaw: opposite signal {bars_since} bars ago")
        
        # Check 4: Price action confirmation (check recent candles for strength)
        if len(data) > 0:
            last_3_candles = data[-3:]
            uptrend = sum(1 for c in last_3_candles if c.get('close', 0) > c.get('open', 0))
            downtrend = sum(1 for c in last_3_candles if c.get('close', 0) < c.get('open', 0))
            
            if action == "BUY" and downtrend >= 2:
                confidence -= 15
                reasons.append("Recent downtrend candles")
            elif action == "SELL" and uptrend >= 2:
                confidence -= 15
                reasons.append("Recent uptrend candles")
        
        # Check 5: Support/Resistance bounce
        current_price = data[-1].get('close', 0) if data else 0
        support = trend_info.get("support", 0)
        resistance = trend_info.get("resistance", 0)
        
        if action == "BUY" and abs(current_price - support) / support < 0.005:
            confidence += 15
            reasons.append("Bounce from support")
        elif action == "SELL" and abs(current_price - resistance) / resistance < 0.005:
            confidence += 15
            reasons.append("Bounce from resistance")
        
        # Clamp confidence to 0-100
        confidence = max(0, min(100, confidence))
        
        # Decision
        is_valid = confidence >= 40  # 40% confidence threshold
        
        return {
            "is_valid": is_valid,
            "confidence": round(confidence, 1),
            "reasons": reasons,
            "filtered": confidence < 40,  # True if filtered out
        }


class DailyStrategyProbabilityAnalyzer:
    """Calculate daily win probability based on recent performance + market conditions"""
    
    @staticmethod
    def analyze(
        strategy_id: str,
        recent_trades: List[Dict[str, Any]],
        market_trend: Dict[str, Any],
        symbol: str,
    ) -> Dict[str, Any]:
        """
        Calculate today's expected win probability for a strategy
        Returns: {probability (%), reason, market_fit, historical_fit, confidence_level}
        """
        if not recent_trades or len(recent_trades) == 0:
            return {
                "probability": 50,
                "confidence_level": "LOW",
                "reason": "No historical data",
                "market_fit": "NEUTRAL",
                "historical_fit": "N/A",
                "samples": 0,
                "recent_win_rate": None,
            }
        
        # Win rate from recent trades
        wins = len([t for t in recent_trades if t.get("pnl", 0) > 0])
        losses = len([t for t in recent_trades if t.get("pnl", 0) < 0])
        total = wins + losses
        historical_wr = (wins / total * 100) if total > 0 else 50
        
        # Market fit: does this strategy work better in certain trends?
        # (This would be trained from historical data; we use heuristic here)
        market_trend_name = market_trend.get("trend", "NEUTRAL")
        market_fit = "NEUTRAL"
        trend_adjustment = 0.0
        
        if market_trend_name == "BULLISH":
            market_fit = "GOOD"  # Most strategies work better in trending markets
            trend_adjustment = 5.0
        elif market_trend_name == "BEARISH":
            market_fit = "POOR"  # Some strategies struggle in downtrends
            trend_adjustment = -5.0
        else:
            market_fit = "MODERATE"
            trend_adjustment = 0.0
        
        # Reversal risk adjustment
        reversal_risk = market_trend.get("reversal_risk", 0.5)
        if reversal_risk > 0.7:
            trend_adjustment -= 10
            market_fit = "POOR"
        
        # Calculate final probability
        final_probability = historical_wr + trend_adjustment
        final_probability = max(20, min(80, final_probability))  # Clamp 20-80
        
        # Confidence in the estimate
        confidence_level = "LOW"
        if total >= 20:
            confidence_level = "HIGH"
        elif total >= 10:
            confidence_level = "MEDIUM"
        
        return {
            "probability": round(final_probability, 1),
            "confidence_level": confidence_level,
            "reason": (
                f"Historical win rate {historical_wr:.0f}% + "
                f"{market_trend_name} market ({trend_adjustment:+.0f}%)"
            ),
            "market_fit": market_fit,
            "historical_fit": "GOOD" if historical_wr > 55 else "POOR" if historical_wr < 45 else "NEUTRAL",
            "samples": total,
            "recent_win_rate": round(historical_wr, 1),
            "historical_wins": wins,
            "historical_losses": losses,
            "adjusted_probability": round(final_probability, 1),
            "recommendation": (
                "PROCEED" if final_probability > 55 else
                "CAUTION" if final_probability > 45 else
                "AVOID"
            ),
        }


class PositionRiskManager:
    """Capital protection + position sizing based on risk parameters"""
    
    @staticmethod
    def calculate_safe_position_size(
        capital: float,
        risk_pct: float,
        entry_price: float,
        stop_loss_price: float,
        max_loss_limit: float = None,
    ) -> Dict[str, Any]:
        """
        Kelly criterion + risk-per-trade sizing
        Returns: {quantity, notional_value, risk_amount, max_loss_at_sl}
        """
        if entry_price <= 0:
            return {"quantity": 0, "error": "Invalid entry price"}
        
        # Risk per trade (typically 1-2% of capital)
        risk_amount = capital * (risk_pct / 100)
        
        # Distance to stop loss
        price_diff = abs(entry_price - stop_loss_price)
        if price_diff <= 0:
            return {"quantity": 0, "error": "SL too close to entry"}
        
        # Quantity calculation
        quantity = int(risk_amount / price_diff)
        
        # Apply daily loss limit
        if max_loss_limit and risk_amount > max_loss_limit:
            quantity = int((max_loss_limit / price_diff))
        
        return {
            "quantity": max(1, quantity),
            "notional_value": round(quantity * entry_price, 2),
            "risk_amount": round(risk_amount, 2),
            "max_loss_at_sl": round(quantity * price_diff, 2),
            "risk_per_trade_pct": round(risk_pct, 2),
        }
    
    @staticmethod
    def check_capital_wipeout_risk(
        current_capital: float,
        open_positions: List[Dict[str, Any]],
        worst_case_loss_pct: float = 5.0,
    ) -> Dict[str, Any]:
        """Check if current positions could wipe out capital in worst case"""
        total_unrealized_loss = sum(p.get("unrealized_pnl", 0) for p in open_positions if p.get("unrealized_pnl", 0) < 0)
        total_notional = sum(abs(p.get("qty", 0)) * p.get("avg_price", 0) for p in open_positions)
        
        if current_capital <= 0:
            return {"risk_level": "CRITICAL", "wipeout_risk_pct": 100}
        
        loss_to_wipeout = current_capital
        worst_case_additional_loss = (total_notional * worst_case_loss_pct / 100)
        
        current_loss_pct = (abs(total_unrealized_loss) / current_capital) * 100 if current_capital else 0
        wipeout_risk_pct = (worst_case_additional_loss / current_capital) * 100 if current_capital else 0
        
        risk_level = "CRITICAL"
        if current_loss_pct > 20:
            risk_level = "CRITICAL"
        elif current_loss_pct > 10:
            risk_level = "HIGH"
        elif wipeout_risk_pct > 5:
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"
        
        return {
            "risk_level": risk_level,
            "current_loss_pct": round(current_loss_pct, 2),
            "wipeout_risk_pct": round(wipeout_risk_pct, 2),
            "total_unrealized_loss": round(total_unrealized_loss, 2),
            "total_notional_exposure": round(total_notional, 2),
            "should_hedge": risk_level in ("CRITICAL", "HIGH"),
            "recommendation": (
                "CLOSE ALL POSITIONS IMMEDIATELY" if risk_level == "CRITICAL" else
                "REDUCE POSITION SIZES" if risk_level == "HIGH" else
                "MONITOR CLOSELY" if risk_level == "MEDIUM" else
                "OK TO TRADE"
            ),
        }


class OrderExecutionRetry:
    """Retry logic for order placement with exponential backoff"""
    
    @staticmethod
    def retry_config(attempt: int = 1) -> Dict[str, Any]:
        """Get retry config for attempt number"""
        max_attempts = 5
        backoff_seconds = min(32, 2 ** (attempt - 1))  # 1, 2, 4, 8, 16, 32
        
        return {
            "attempt": attempt,
            "max_attempts": max_attempts,
            "backoff_seconds": backoff_seconds,
            "should_retry": attempt < max_attempts,
            "retry_reason_hints": [
                "Network timeout - trying again",
                "Broker API busy - waiting before retry",
                "Rate limit hit - exponential backoff",
                "Connection reset - reconnecting",
                "Order validation failed - check order details",
            ],
        }
    
    @staticmethod
    def is_retryable_error(error_msg: str) -> bool:
        """Determine if an error is retryable"""
        retryable_keywords = [
            "timeout", "connection", "busy", "rate limit", "temporarily",
            "unavailable", "network", "try again", "try_again", "retry"
        ]
        error_lower = error_msg.lower()
        return any(keyword in error_lower for keyword in retryable_keywords)


class PositionRecovery:
    """Recover from disconnects - reconcile broker positions with local state"""
    
    @staticmethod
    def reconcile_positions(
        broker_positions: List[Dict[str, Any]],
        local_positions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Compare broker positions with local tracking
        Returns: {matched, missing_broker, extra_local, reconciliation_actions}
        """
        broker_by_symbol = {p["symbol"]: p for p in broker_positions}
        local_by_symbol = {p["symbol"]: p for p in local_positions}
        
        matched = []
        missing_broker = []  # In local but not in broker
        extra_local = []  # In broker but not in local
        reconciliation_actions = []
        
        # Check all local positions
        for symbol, local_pos in local_by_symbol.items():
            if symbol in broker_by_symbol:
                broker_pos = broker_by_symbol[symbol]
                if local_pos["qty"] == broker_pos["qty"]:
                    matched.append({"symbol": symbol, "qty": local_pos["qty"], "status": "OK"})
                else:
                    # Quantity mismatch
                    delta = broker_pos["qty"] - local_pos["qty"]
                    reconciliation_actions.append({
                        "action": "UPDATE_LOCAL",
                        "symbol": symbol,
                        "reason": f"Qty mismatch: local={local_pos['qty']}, broker={broker_pos['qty']}",
                        "new_qty": broker_pos["qty"],
                        "delta": delta,
                    })
            else:
                # Missing in broker - position was closed
                missing_broker.append({
                    "symbol": symbol,
                    "qty": local_pos["qty"],
                    "reason": "Position not found on broker (closed externally?)",
                })
                reconciliation_actions.append({
                    "action": "REMOVE_LOCAL",
                    "symbol": symbol,
                    "reason": "Position closed on broker",
                })
        
        # Check all broker positions
        for symbol, broker_pos in broker_by_symbol.items():
            if symbol not in local_by_symbol:
                extra_local.append({
                    "symbol": symbol,
                    "qty": broker_pos["qty"],
                    "reason": "Position not tracked locally (opened externally?)",
                })
                reconciliation_actions.append({
                    "action": "ADD_LOCAL",
                    "symbol": symbol,
                    "qty": broker_pos["qty"],
                    "reason": "Position opened on broker",
                })
        
        return {
            "matched_count": len(matched),
            "matched": matched,
            "missing_broker_count": len(missing_broker),
            "missing_broker": missing_broker,
            "extra_local_count": len(extra_local),
            "extra_local": extra_local,
            "reconciliation_needed": len(reconciliation_actions) > 0,
            "reconciliation_actions": reconciliation_actions,
        }


class AutoExitManager:
    """Automatic exit logic: take profit, stop loss, breakeven, trailing stop"""
    
    @staticmethod
    def check_exit_triggers(
        position: Dict[str, Any],
        current_price: float,
        exit_config: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """
        Check if any exit conditions are met
        Returns: {should_exit, exit_reason, exit_type, target_price}
        """
        if exit_config is None:
            exit_config = {}
        
        avg_price = position.get("avg_price", 0)
        qty = position.get("qty", 0)
        unrealized_pnl = (current_price - avg_price) * qty if avg_price > 0 else 0
        unrealized_pnl_pct = ((current_price - avg_price) / avg_price * 100) if avg_price > 0 else 0
        
        triggers = []
        
        # 1. Take Profit
        tp_pct = exit_config.get("take_profit_pct", 0)
        if tp_pct > 0 and unrealized_pnl_pct >= tp_pct:
            triggers.append({
                "type": "TAKE_PROFIT",
                "pnl_pct": unrealized_pnl_pct,
                "target_pct": tp_pct,
                "price": current_price,
            })
        
        # 2. Stop Loss
        sl_pct = exit_config.get("stop_loss_pct", 0)
        if sl_pct > 0 and unrealized_pnl_pct <= -sl_pct:
            triggers.append({
                "type": "STOP_LOSS",
                "pnl_pct": unrealized_pnl_pct,
                "target_pct": -sl_pct,
                "price": current_price,
            })
        
        # 3. Breakeven + cushion
        be_cushion_pct = exit_config.get("breakeven_cushion_pct", 0)
        if be_cushion_pct > 0 and unrealized_pnl_pct >= be_cushion_pct:
            triggers.append({
                "type": "BREAKEVEN_STOP",
                "pnl_pct": unrealized_pnl_pct,
                "cushion_pct": be_cushion_pct,
                "price": current_price,
            })
        
        # 4. Trailing Stop
        trailing_pct = exit_config.get("trailing_stop_pct", 0)
        if trailing_pct > 0 and unrealized_pnl_pct > 0:
            highest_pnl = position.get("highest_pnl_pct", unrealized_pnl_pct)
            drawdown = highest_pnl - unrealized_pnl_pct
            if drawdown >= trailing_pct:
                triggers.append({
                    "type": "TRAILING_STOP",
                    "highest_pnl_pct": highest_pnl,
                    "current_pnl_pct": unrealized_pnl_pct,
                    "drawdown_pct": drawdown,
                    "trailing_pct": trailing_pct,
                    "price": current_price,
                })
        
        should_exit = len(triggers) > 0
        exit_reason = triggers[0]["type"] if triggers else None
        
        return {
            "should_exit": should_exit,
            "exit_reason": exit_reason,
            "exit_type": triggers[0].get("type") if triggers else None,
            "current_price": current_price,
            "unrealized_pnl": round(unrealized_pnl, 2),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
            "triggers": triggers,
        }

