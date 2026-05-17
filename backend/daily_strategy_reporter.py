"""
Daily Strategy Performance Probability Analyzer

Generates daily updated descriptions showing:
- Today's expected win probability
- Market conditions fit
- Historical performance trend
- Risk-adjusted return potential
"""

from typing import Dict, Any, List
from datetime import datetime, timedelta
import math


class DailyStrategyReporter:
    """Generate daily strategy performance reports with probability"""
    
    @staticmethod
    def generate_daily_report(
        strategy_id: str,
        strategy_name: str,
        underlying: str,
        recent_trades: List[Dict[str, Any]],
        market_trend_analysis: Dict[str, Any],
        total_capital: float = 100000,
    ) -> Dict[str, Any]:
        """
        Generate comprehensive daily report for a strategy
        """
        # Historical performance
        if recent_trades:
            wins = len([t for t in recent_trades if t.get("pnl", 0) > 0])
            losses = len([t for t in recent_trades if t.get("pnl", 0) < 0])
            total_trades = len(recent_trades)
            win_rate = (wins / total_trades * 100) if total_trades > 0 else 50
            avg_win = sum(t.get("pnl", 0) for t in recent_trades if t.get("pnl", 0) > 0) / wins if wins > 0 else 0
            avg_loss = abs(sum(t.get("pnl", 0) for t in recent_trades if t.get("pnl", 0) < 0) / losses) if losses > 0 else 0
            profit_factor = (abs(sum(t.get("pnl", 0) for t in recent_trades if t.get("pnl", 0) > 0)) / 
                            abs(sum(t.get("pnl", 0) for t in recent_trades if t.get("pnl", 0) < 0))) if losses > 0 else 0
            total_pnl = sum(t.get("pnl", 0) for t in recent_trades)
            return_pct = (total_pnl / total_capital * 100) if total_capital else 0
        else:
            wins = losses = total_trades = 0
            win_rate = avg_win = avg_loss = profit_factor = total_pnl = return_pct = 0
        
        # Market fit analysis
        market_trend = market_trend_analysis.get("trend", "NEUTRAL")
        rsi = market_trend_analysis.get("rsi", 50)
        atr = market_trend_analysis.get("atr", 0)
        reversal_risk = market_trend_analysis.get("reversal_risk", 0.5)
        
        # Strategy type detection (heuristic)
        strategy_type = DailyStrategyReporter._detect_strategy_type(strategy_name)
        
        # Today's probability calculation
        today_probability = DailyStrategyReporter._calculate_today_probability(
            win_rate=win_rate,
            market_trend=market_trend,
            rsi=rsi,
            strategy_type=strategy_type,
            reversal_risk=reversal_risk,
        )
        
        # Market fit assessment
        market_fit_score = DailyStrategyReporter._assess_market_fit(
            strategy_type=strategy_type,
            market_trend=market_trend,
            rsi=rsi,
            atr=atr,
        )
        
        # Generate description
        description = DailyStrategyReporter._generate_description(
            strategy_name=strategy_name,
            strategy_type=strategy_type,
            today_probability=today_probability,
            market_fit_score=market_fit_score,
            win_rate=win_rate,
            market_trend=market_trend,
            total_trades=total_trades,
            underlying=underlying,
        )
        
        return {
            "strategy_id": strategy_id,
            "strategy_name": strategy_name,
            "underlying": underlying,
            "date_generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
            
            # Historical metrics
            "historical": {
                "total_trades": total_trades,
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 1),
                "avg_win_pnl": round(avg_win, 2),
                "avg_loss_pnl": round(avg_loss, 2),
                "profit_factor": round(profit_factor, 2),
                "total_pnl": round(total_pnl, 2),
                "return_pct": round(return_pct, 2),
            },
            
            # Today's market conditions
            "market_conditions": {
                "trend": market_trend,
                "rsi": round(rsi, 1),
                "atr": round(atr, 2),
                "reversal_risk": round(reversal_risk, 2),
            },
            
            # Today's probability
            "today": {
                "expected_win_probability": round(today_probability, 1),
                "market_fit_score": round(market_fit_score, 1),
                "recommendation": (
                    "🟢 HIGH" if today_probability > 65 else
                    "🟡 MODERATE" if today_probability > 50 else
                    "🔴 LOW"
                ),
                "strategy_type": strategy_type,
            },
            
            # Daily description
            "daily_description": description,
            
            # Detailed breakdown
            "breakdown": {
                "strategy_fit_score": DailyStrategyReporter._strategy_fit_to_market(strategy_type, market_trend),
                "volatility_assessment": DailyStrategyReporter._volatility_assessment(atr, strategy_type),
                "rsi_assessment": DailyStrategyReporter._rsi_assessment(rsi, strategy_type),
                "risk_factors": DailyStrategyReporter._identify_risk_factors(market_trend, rsi, reversal_risk),
                "opportunities": DailyStrategyReporter._identify_opportunities(market_trend, rsi, win_rate),
            },
        }
    
    @staticmethod
    def _detect_strategy_type(strategy_name: str) -> str:
        """Detect strategy type from name"""
        name_lower = strategy_name.lower()
        
        if "momentum" in name_lower or "ema" in name_lower:
            return "MOMENTUM"
        elif "mean reversion" in name_lower or "rsi" in name_lower or "reversion" in name_lower:
            return "MEAN_REVERSION"
        elif "breakout" in name_lower:
            return "BREAKOUT"
        elif "trend" in name_lower:
            return "TREND_FOLLOWING"
        elif "range" in name_lower:
            return "RANGE_BOUND"
        else:
            return "CUSTOM"
    
    @staticmethod
    def _calculate_today_probability(
        win_rate: float,
        market_trend: str,
        rsi: float,
        strategy_type: str,
        reversal_risk: float,
    ) -> float:
        """Calculate today's expected win probability"""
        
        # Base probability from historical win rate
        base_prob = win_rate if win_rate > 0 else 50
        
        # Trend adjustment based on strategy type
        trend_adjustment = 0.0
        if strategy_type == "MOMENTUM":
            if market_trend == "BULLISH":
                trend_adjustment = 10
            elif market_trend == "BEARISH":
                trend_adjustment = -5
            else:
                trend_adjustment = -10  # Momentum struggles in range
        
        elif strategy_type == "MEAN_REVERSION":
            if market_trend == "NEUTRAL":
                trend_adjustment = 10  # Thrives in range
            elif market_trend == "BULLISH":
                trend_adjustment = -5  # Less effective in strong trends
            elif market_trend == "BEARISH":
                trend_adjustment = -5
        
        elif strategy_type == "BREAKOUT":
            if market_trend in ("BULLISH", "BEARISH"):
                trend_adjustment = 15  # Thrives in strong trends
            else:
                trend_adjustment = -15  # Struggles in range
        
        elif strategy_type == "TREND_FOLLOWING":
            if market_trend in ("BULLISH", "BEARISH"):
                trend_adjustment = 12
            else:
                trend_adjustment = -8
        
        elif strategy_type == "RANGE_BOUND":
            if market_trend == "NEUTRAL":
                trend_adjustment = 8
            else:
                trend_adjustment = -12
        
        # RSI assessment
        rsi_adjustment = 0.0
        if strategy_type == "MEAN_REVERSION":
            if rsi < 30 or rsi > 70:
                rsi_adjustment = 8  # Extreme RSI = good for mean reversion
            elif 40 < rsi < 60:
                rsi_adjustment = -5  # Middle range = bad for mean reversion
        
        elif strategy_type in ("MOMENTUM", "BREAKOUT", "TREND_FOLLOWING"):
            if (market_trend == "BULLISH" and rsi > 50) or (market_trend == "BEARISH" and rsi < 50):
                rsi_adjustment = 5
            else:
                rsi_adjustment = -3
        
        # Reversal risk penalty
        reversal_penalty = reversal_risk * 20  # Up to 20% penalty if high reversal risk
        
        # Calculate final probability
        final_prob = base_prob + trend_adjustment + rsi_adjustment - reversal_penalty
        
        # Clamp to 20-80 (realistic range)
        final_prob = max(20, min(80, final_prob))
        
        return final_prob
    
    @staticmethod
    def _assess_market_fit(
        strategy_type: str,
        market_trend: str,
        rsi: float,
        atr: float,
    ) -> float:
        """Score market fit 0-100"""
        score = 50  # Start neutral
        
        # Trend fit
        if strategy_type == "MOMENTUM" and market_trend in ("BULLISH", "BEARISH"):
            score += 20
        elif strategy_type == "MEAN_REVERSION" and market_trend == "NEUTRAL":
            score += 20
        elif strategy_type == "BREAKOUT" and market_trend in ("BULLISH", "BEARISH"):
            score += 20
        elif strategy_type == "TREND_FOLLOWING" and market_trend in ("BULLISH", "BEARISH"):
            score += 15
        
        # RSI fit
        if 30 < rsi < 70:
            score += 10  # Normal
        elif (rsi < 30 or rsi > 70) and strategy_type == "MEAN_REVERSION":
            score += 15  # Extreme RSI good for mean reversion
        elif (rsi < 30 or rsi > 70) and strategy_type in ("MOMENTUM", "BREAKOUT"):
            score -= 5  # Extreme RSI means limited upside/downside
        
        return max(0, min(100, score))
    
    @staticmethod
    def _generate_description(
        strategy_name: str,
        strategy_type: str,
        today_probability: float,
        market_fit_score: float,
        win_rate: float,
        market_trend: str,
        total_trades: int,
        underlying: str,
    ) -> str:
        """Generate human-readable daily description"""
        
        probability_text = (
            "🟢 HIGH probability (>65%)" if today_probability > 65 else
            "🟡 MODERATE probability (50-65%)" if today_probability > 50 else
            "🔴 LOW probability (<50%)"
        )
        
        if total_trades == 0:
            history_text = "No historical data yet"
        else:
            history_text = f"{total_trades} trades, {win_rate:.0f}% win rate"
        
        fit_text = (
            "excellent fit" if market_fit_score > 70 else
            "good fit" if market_fit_score > 55 else
            "moderate fit" if market_fit_score > 40 else
            "poor fit"
        )
        
        trend_text = f"{market_trend} market conditions"
        
        return (
            f"{strategy_name} on {underlying}: "
            f"{probability_text} today ({today_probability:.0f}%). "
            f"{history_text}. "
            f"{fit_text} with {trend_text}. "
            f"Market fit score: {market_fit_score:.0f}/100."
        )
    
    @staticmethod
    def _strategy_fit_to_market(strategy_type: str, market_trend: str) -> Dict[str, Any]:
        """Detailed strategy-market fit analysis"""
        
        compatibility_matrix = {
            "MOMENTUM": {
                "BULLISH": {"fit": "EXCELLENT", "reason": "Momentum thrives in uptrends"},
                "BEARISH": {"fit": "GOOD", "reason": "Momentum works in downtrends too"},
                "NEUTRAL": {"fit": "POOR", "reason": "No momentum in ranging markets"},
            },
            "MEAN_REVERSION": {
                "BULLISH": {"fit": "POOR", "reason": "Trend too strong, SL gets hit"},
                "BEARISH": {"fit": "POOR", "reason": "Trend too strong against reversions"},
                "NEUTRAL": {"fit": "EXCELLENT", "reason": "Mean reversion perfect for ranges"},
            },
            "BREAKOUT": {
                "BULLISH": {"fit": "EXCELLENT", "reason": "Continuation breakouts likely"},
                "BEARISH": {"fit": "EXCELLENT", "reason": "Downside breakouts likely"},
                "NEUTRAL": {"fit": "POOR", "reason": "Fake breakouts common in ranges"},
            },
            "TREND_FOLLOWING": {
                "BULLISH": {"fit": "EXCELLENT", "reason": "Follow the uptrend"},
                "BEARISH": {"fit": "EXCELLENT", "reason": "Follow the downtrend"},
                "NEUTRAL": {"fit": "POOR", "reason": "No trend to follow"},
            },
            "RANGE_BOUND": {
                "BULLISH": {"fit": "POOR", "reason": "Uptrend too strong"},
                "BEARISH": {"fit": "POOR", "reason": "Downtrend too strong"},
                "NEUTRAL": {"fit": "EXCELLENT", "reason": "Range-bound strategy ideal"},
            },
        }
        
        return compatibility_matrix.get(strategy_type, {}).get(market_trend, {"fit": "UNKNOWN", "reason": "No data"})
    
    @staticmethod
    def _volatility_assessment(atr: float, strategy_type: str) -> Dict[str, Any]:
        """Assess if current volatility is good for this strategy"""
        
        # ATR interpretation (this is relative to the underlying)
        volatility_level = (
            "HIGH" if atr > 100 else
            "MODERATE" if atr > 50 else
            "LOW"
        )
        
        assessment = {
            "MOMENTUM": {
                "HIGH": "✅ Ideal - momentum strategies thrive in high volatility",
                "MODERATE": "✅ Good - normal volatility",
                "LOW": "⚠️ Challenging - low volatility reduces signal quality",
            },
            "MEAN_REVERSION": {
                "HIGH": "⚠️ Challenging - need extreme moves for reversions",
                "MODERATE": "✅ Good - moderate range for reversions",
                "LOW": "✅ Ideal - easier to identify reversions in low vol",
            },
            "BREAKOUT": {
                "HIGH": "✅ Ideal - need volatility for breakouts",
                "MODERATE": "✅ Good - breakouts possible",
                "LOW": "⚠️ Challenging - hard to identify true breakouts",
            },
            "TREND_FOLLOWING": {
                "HIGH": "✅ Ideal - trends show in volatility",
                "MODERATE": "✅ Good - trends detectable",
                "LOW": "⚠️ Challenging - trends unclear in low vol",
            },
            "RANGE_BOUND": {
                "HIGH": "⚠️ Challenging - range breaks down in high vol",
                "MODERATE": "✅ Good - moderate volatility",
                "LOW": "✅ Ideal - stable ranges in low volatility",
            },
        }
        
        return {
            "atr": round(atr, 2),
            "level": volatility_level,
            "assessment": assessment.get(strategy_type, {}).get(volatility_level, "No data"),
        }
    
    @staticmethod
    def _rsi_assessment(rsi: float, strategy_type: str) -> Dict[str, Any]:
        """Assess RSI suitability for strategy"""
        
        rsi_zone = (
            "OVERSOLD" if rsi < 30 else
            "NEUTRAL_LOW" if rsi < 40 else
            "NEUTRAL" if rsi < 60 else
            "NEUTRAL_HIGH" if rsi < 70 else
            "OVERBOUGHT"
        )
        
        assessment = {
            "MOMENTUM": {
                "OVERSOLD": "💡 Momentum builders look for overbought",
                "NEUTRAL_LOW": "✅ Good for momentum",
                "NEUTRAL": "✅ Neutral zone",
                "NEUTRAL_HIGH": "✅ Building momentum",
                "OVERBOUGHT": "✅ Ideal for momentum continuation",
            },
            "MEAN_REVERSION": {
                "OVERSOLD": "✅ Perfect - reversal opportunity",
                "NEUTRAL_LOW": "⚠️ Not extreme enough yet",
                "NEUTRAL": "❌ Too neutral for mean reversion",
                "NEUTRAL_HIGH": "⚠️ Not extreme enough yet",
                "OVERBOUGHT": "✅ Perfect - reversal opportunity",
            },
        }
        
        return {
            "rsi": round(rsi, 1),
            "zone": rsi_zone,
            "assessment": assessment.get(strategy_type, {}).get(rsi_zone, "Check signals"),
        }
    
    @staticmethod
    def _identify_risk_factors(
        market_trend: str,
        rsi: float,
        reversal_risk: float,
    ) -> List[str]:
        """Identify today's risk factors"""
        
        factors = []
        
        if reversal_risk > 0.75:
            factors.append("🔴 HIGH reversal risk - trend may be exhausted")
        
        if market_trend == "NEUTRAL":
            factors.append("⚠️ Range-bound market - choppy conditions expected")
        
        if rsi > 70:
            factors.append("⚠️ Overbought conditions - profit-taking risk")
        
        if rsi < 30:
            factors.append("⚠️ Oversold conditions - reversal risk")
        
        if 40 < rsi < 60:
            factors.append("⚠️ Indecisive RSI - unclear direction")
        
        return factors if factors else ["✅ No major risk factors detected"]
    
    @staticmethod
    def _identify_opportunities(
        market_trend: str,
        rsi: float,
        win_rate: float,
    ) -> List[str]:
        """Identify today's opportunities"""
        
        opportunities = []
        
        if market_trend in ("BULLISH", "BEARISH"):
            opportunities.append(f"🟢 Strong {market_trend} trend - directional trades favored")
        
        if 30 < rsi < 70:
            opportunities.append("🟢 Neutral RSI - balanced risk/reward")
        
        if win_rate > 60:
            opportunities.append(f"🟢 Historical win rate {win_rate:.0f}% - high confidence")
        
        if rsi < 30 or rsi > 70:
            opportunities.append("🟢 Extreme RSI - reversal or breakout likely")
        
        return opportunities if opportunities else ["⚠️ Limited opportunities today"]

