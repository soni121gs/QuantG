"""Deterministic, no-lookahead historical market regime labels."""
from __future__ import annotations

from typing import Any, Dict, List


def tag_regimes(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    closes: List[float] = []
    returns: List[float] = []
    for candle in candles:
        close = float(candle.get("close") or 0)
        prev = closes[-1] if closes else close
        ret = (close / prev - 1.0) if prev else 0.0
        closes.append(close)
        returns.append(ret)
        history = closes[:-1]
        sma20 = sum(history[-20:]) / len(history[-20:]) if history else close
        rv20 = (sum(r * r for r in returns[:-1][-20:]) / max(1, len(returns[:-1][-20:]))) ** 0.5
        trend = "UPTREND" if close > sma20 * 1.01 else "DOWNTREND" if close < sma20 * 0.99 else "RANGE"
        vol = "HIGH_VOL" if abs(ret) > max(0.012, rv20 * 1.5) else "LOW_VOL"
        gap = abs(float(candle.get("open") or close) / prev - 1.0) > 0.01 if prev else False
        out.append({"date": str(candle.get("date"))[:10], "trend": trend, "vol": vol,
                    "gap_day": gap, "large_move_day": abs(ret) > 0.015})
    return out


def aggregate_by_regime(trades: List[Dict[str, Any]], regimes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    buckets: Dict[str, List[float]] = {}
    for trade in trades:
        regime = regimes.get(str(trade.get("entry_day") or trade.get("date"))[:10], {})
        key = f"{regime.get('trend', 'UNKNOWN')}|{regime.get('vol', 'UNKNOWN')}"
        buckets.setdefault(key, []).append(float(trade.get("pnl") or 0))
    return {key: {"n": len(vals), "expectancy": round(sum(vals) / len(vals), 2),
                  "win_rate": round(sum(v > 0 for v in vals) / len(vals), 3)}
            for key, vals in buckets.items()}
