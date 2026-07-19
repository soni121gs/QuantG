"""ERP P3-2 daily delta-1 momentum research sleeve.

Builds daily time-series momentum signals for the EOD option judge. The vehicle
is a deep-ITM single option (`itm_offset_pct`) so the backtest is closer to
delta-1 participation than the old OTM premium buyers.
"""
from __future__ import annotations

from statistics import pstdev
from typing import Any, Dict, List, Optional

from core.bhavcopy_store import BhavcopyStore
from core.edge_research_ledger import deflated_sharpe
from core.eod_options_backtest import EODOptionsBacktest, walk_forward


DEFAULT_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"]


def realized_vol_pct(candles: List[Dict[str, Any]], idx: int, lookback: int = 20) -> Optional[float]:
    if idx < lookback:
        return None
    closes = [float(c["close"]) for c in candles[idx - lookback: idx + 1]]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) < 5:
        return None
    return pstdev(rets) * (252 ** 0.5) * 100


def vol_target_lots(vol_pct: Optional[float], *, target_vol_pct: float = 12.0,
                    min_lots: int = 1, max_lots: int = 3) -> int:
    if not vol_pct or vol_pct <= 0:
        return min_lots
    raw = round(target_vol_pct / vol_pct)
    return max(min_lots, min(max_lots, int(raw)))


def build_signals(candles: List[Dict[str, Any]], *, lookback: int = 20,
                  hold_days: int = 5, variant: str = "close_to_close") -> Dict[str, Any]:
    signals: List[Dict[str, Any]] = []
    for i in range(lookback, max(lookback, len(candles) - hold_days)):
        c = candles[i]
        close = float(c.get("close") or 0)
        prev = float(candles[i - lookback].get("close") or 0)
        if not close or not prev:
            continue
        if variant == "overnight":
            open_px = float(c.get("open") or close)
            prev_close = float(candles[i - 1].get("close") or open_px)
            mom = (open_px - prev_close) / prev_close if prev_close else 0.0
            threshold = 0.002
        else:
            mom = (close - prev) / prev
            threshold = 0.01
        if abs(mom) < threshold:
            continue
        vol = realized_vol_pct(candles, i, lookback)
        direction = "CE" if mom > 0 else "PE"
        signals.append({
            "date": c["date"],
            "action": "BUY" if direction == "CE" else "SELL",
            "direction": direction,
            "momentum_pct": round(mom * 100, 3),
            "realized_vol_pct": round(vol, 2) if vol is not None else None,
            "recommended_lots": vol_target_lots(vol),
            "variant": variant,
        })
    return {"signals": signals, "audit": {"variant": variant, "lookback": lookback,
                                          "hold_days": hold_days, "accepted": len(signals)}}


def strategy_config(underlying: str, *, itm_offset_pct: float = 0.02) -> Dict[str, Any]:
    return {
        "id": f"S3-DELTA1-{underlying.upper()}",
        "name": f"S3 Daily Delta-1 Momentum {underlying.upper()}",
        "python_code": "def run(data):\n    return []\n",
        "visual_config": {"symbol": underlying.upper(), "options": {
            "underlying": underlying.upper(),
            "structure": "single_leg",
            "strike_mode": "ITM_BUY",
            "itm_offset_pct": itm_offset_pct,
            "exit_mode": "",
            "lots": 1,
        }, "risk": {"target_pct": 60.0, "stoploss_pct": 25.0, "max_hold_days": 5}},
    }


def grade_underlying(underlying: str, *, store: Optional[BhavcopyStore] = None,
                     start: Optional[str] = None, end: Optional[str] = None,
                     variant: str = "close_to_close") -> Dict[str, Any]:
    store = store or BhavcopyStore()
    candles = store.underlying_daily(underlying.upper(), start, end)
    if len(candles) < 80:
        return {"underlying": underlying.upper(), "status": "error",
                "error": f"insufficient bhavcopy history for {underlying.upper()} ({len(candles)} days)"}
    sig = build_signals(candles, variant=variant)
    params = {"min_dte": 14, "max_dte": 45, "max_hold_days": 5, "itm_offset_pct": 0.02,
              "debit_tp": 0.6, "debit_sl": 0.25}
    res = EODOptionsBacktest(store).run(
        strategy_config(underlying), start=start, end=end, params=params, signals=sig["signals"],
    )
    if res.get("error"):
        return {"underlying": underlying.upper(), "variant": variant, "status": "error", "error": res["error"]}
    wf = walk_forward(res)
    overall = wf.get("overall") or {}
    dsr = deflated_sharpe({
        "n": overall.get("n"),
        "expectancy": overall.get("expectancy"),
        "oos_expectancy": (wf.get("oos") or {}).get("expectancy"),
        "pct_green_months": wf.get("pct_green_months"),
    })
    regimes = res.get("regime_breakdown") or {}
    regime_gate = {"passed": len([v for v in regimes.values() if (v or {}).get("n", 0) > 0]) >= 2,
                   "regime_count": len([v for v in regimes.values() if (v or {}).get("n", 0) > 0])}
    return {
        "underlying": underlying.upper(), "variant": variant, "status": "ready",
        "selector": sig["audit"], "verdict": wf.get("verdict"), "overall": overall,
        "oos": wf.get("oos"), "oos_year": wf.get("oos_year"),
        "pct_green_months": wf.get("pct_green_months"), "regime_breakdown": regimes,
        "paper_gate": {"eligible_for_paper": bool(
            wf.get("verdict") == "CANDIDATE_EDGE" and dsr.get("passed") and regime_gate["passed"]
        ), "deflated_sharpe": dsr, "regime_gate": regime_gate},
    }


def grade_universe(underlyings: Optional[List[str]] = None, **kwargs: Any) -> Dict[str, Any]:
    if kwargs.get("store") is None:
        kwargs["store"] = BhavcopyStore()
    rows = []
    for u in (underlyings or DEFAULT_UNDERLYINGS):
        rows.append(grade_underlying(u, variant="close_to_close", **kwargs))
        rows.append(grade_underlying(u, variant="overnight", **kwargs))
    return {"count": len(rows), "eligible_for_paper": sum(
        1 for r in rows if (r.get("paper_gate") or {}).get("eligible_for_paper")), "rows": rows}
