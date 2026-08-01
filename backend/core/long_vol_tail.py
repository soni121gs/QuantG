"""Long-vol / tail-protection research sleeve (the one mechanism the book lacks).

The whole live book is short premium / short gamma, so it bleeds on the ~1% of
days the index gaps or trends hard (the RANGE-seller's structural blind spot,
CLAUDE.md §18). This sleeve is the mirror: BUY cheap convexity that PAYS on those
tail days. The vehicle is a long OTM index PUT held to expiry — the dominant index
tail is a downside gap — optionally gated to LOW realized vol so we buy insurance
when it is cheap (the inverse of the seller's IV-rich gate).

Research-only. It does NOT wake a registry row or touch any broker path. Two
questions are graded honestly and separately:

  1. Standalone expectancy (walk-forward OOS). Insurance normally costs money, so a
     NO_EDGE_NEGATIVE here is expected and is NOT a reason to discard it.
  2. Convexity / hedge value — does it actually pay on the tail? Reported as the
     max single-trade payoff, the % of premium recovered, and the count of
     >2x-premium "tail hits". A tail hedge earns its place on (2), not (1).
"""
from __future__ import annotations

from statistics import pstdev
from typing import Any, Dict, List, Optional

from core.bhavcopy_store import BhavcopyStore
from core.edge_research_ledger import deflated_sharpe
from core.eod_options_backtest import EODOptionsBacktest, walk_forward


DEFAULT_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"]


def _realized_vol_pct(candles: List[Dict[str, Any]], idx: int, lookback: int = 20) -> Optional[float]:
    if idx < lookback:
        return None
    closes = [float(c["close"]) for c in candles[idx - lookback: idx + 1]]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) < 5:
        return None
    return pstdev(rets) * (252 ** 0.5) * 100


def build_tail_signals(candles: List[Dict[str, Any]], *, entry_stride_days: int = 5,
                       cheap_vol_max_pct: Optional[float] = None,
                       lookback: int = 20) -> Dict[str, Any]:
    """Buy a long OTM PUT every `entry_stride_days`. If `cheap_vol_max_pct` is set,
    only enter when trailing realized vol is BELOW it (buy insurance while cheap)."""
    signals: List[Dict[str, Any]] = []
    rejected = {"stride_skip": 0, "vol_not_cheap": 0, "no_vol": 0}
    stride = max(1, int(entry_stride_days or 1))
    for idx, c in enumerate(candles):
        if idx % stride != 0:
            rejected["stride_skip"] += 1
            continue
        if cheap_vol_max_pct is not None:
            vol = _realized_vol_pct(candles, idx, lookback)
            if vol is None:
                rejected["no_vol"] += 1
                continue
            if vol > cheap_vol_max_pct:
                rejected["vol_not_cheap"] += 1
                continue
        else:
            vol = _realized_vol_pct(candles, idx, lookback)
        signals.append({
            "date": c["date"], "action": "BUY", "direction": "PE",
            "realized_vol_pct": round(vol, 2) if vol is not None else None,
            "gate": "cheap_vol" if cheap_vol_max_pct is not None else "unconditional",
        })
    return {"signals": signals, "audit": {"entry_stride_days": stride,
                                          "cheap_vol_max_pct": cheap_vol_max_pct,
                                          "accepted": len(signals), "rejected": rejected}}


def strategy_config(underlying: str, *, otm_offset_pct: float) -> Dict[str, Any]:
    return {
        "id": f"LV-TAIL-{underlying.upper()}",
        "name": f"Long-Vol Tail Put {underlying.upper()} {otm_offset_pct*100:.0f}%OTM",
        "python_code": "def run(data):\n    return []\n",
        "visual_config": {"symbol": underlying.upper(), "options": {
            "underlying": underlying.upper(),
            "structure": "single_leg",
            "otm_offset_pct": otm_offset_pct,
            "exit_mode": "expiry",   # hold the tail leg to expiry; collect intrinsic
            "lots": 1,
        }, "risk": {"exit_mode": "hold_to_expiry", "max_hold_days": 8}},
    }


def _tail_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Convexity signature: does the hedge pay on the tail? Uses realized pnl per trade."""
    if not trades:
        return {"n": 0}
    pnls = [float(t.get("pnl") or t.get("realized_pnl") or 0.0) for t in trades]
    prems = [abs(float(t.get("entry_basis") or t.get("entry_ref") or 0.0)) for t in trades]
    total_prem = sum(prems) or 1.0
    gross_wins = sum(p for p in pnls if p > 0)
    tail_hits = sum(1 for i, p in enumerate(pnls) if prems[i] and p > 2 * prems[i])
    return {
        "n": len(pnls),
        "max_single_payoff": round(max(pnls), 1),
        "pct_premium_recovered": round(100 * gross_wins / total_prem, 1),
        "tail_hits_gt_2x": tail_hits,
        "win_rate": round(100 * sum(1 for p in pnls if p > 0) / len(pnls), 1),
    }


def grade_underlying(underlying: str, *, store: Optional[BhavcopyStore] = None,
                     start: Optional[str] = None, end: Optional[str] = None,
                     otm_offset_pct: float = 0.05, entry_stride_days: int = 5,
                     cheap_vol_max_pct: Optional[float] = None) -> Dict[str, Any]:
    store = store or BhavcopyStore()
    candles = store.underlying_daily(underlying.upper(), start, end)
    if len(candles) < 80:
        return {"underlying": underlying.upper(), "status": "error",
                "error": f"insufficient bhavcopy history for {underlying.upper()} ({len(candles)} days)"}
    sig = build_tail_signals(candles, entry_stride_days=entry_stride_days,
                             cheap_vol_max_pct=cheap_vol_max_pct)
    params = {"otm_offset_pct": otm_offset_pct, "min_dte": 2, "max_dte": 8,
              "max_hold_days": 8, "exit_mode": "expiry"}
    res = EODOptionsBacktest(store).run(strategy_config(underlying, otm_offset_pct=otm_offset_pct),
                                        start=start, end=end, params=params, signals=sig["signals"])
    if res.get("error"):
        return {"underlying": underlying.upper(), "status": "error", "error": res["error"],
                "selector": sig["audit"]}
    wf = walk_forward(res)
    overall = wf.get("overall") or {}
    dsr = deflated_sharpe({
        "n": overall.get("n"), "expectancy": overall.get("expectancy"),
        "oos_expectancy": (wf.get("oos") or {}).get("expectancy"),
        "pct_green_months": wf.get("pct_green_months"),
    })
    return {
        "underlying": underlying.upper(), "status": "ready",
        "gate": sig["audit"].get("cheap_vol_max_pct"), "selector": sig["audit"],
        "verdict": wf.get("verdict"), "overall": overall, "oos": wf.get("oos"),
        "oos_year": wf.get("oos_year"), "pct_green_months": wf.get("pct_green_months"),
        "tail_metrics": _tail_metrics(res.get("trades") or []),
        # A tail hedge is graded on convexity, not standalone expectancy. eligible_for_paper
        # here means "pays on the tail AND does not bleed the whole book" — deliberately strict.
        "paper_gate": {
            "eligible_for_paper": bool(
                wf.get("verdict") in ("CANDIDATE_EDGE", "FRAGILE")
                and _tail_metrics(res.get("trades") or []).get("tail_hits_gt_2x", 0) >= 3
            ),
            "deflated_sharpe": dsr,
        },
    }


def grade_universe(underlyings: Optional[List[str]] = None, **kwargs: Any) -> Dict[str, Any]:
    if kwargs.get("store") is None:
        kwargs["store"] = BhavcopyStore()
    rows: List[Dict[str, Any]] = []
    for u in (underlyings or DEFAULT_UNDERLYINGS):
        # unconditional insurance cost
        rows.append(grade_underlying(u, cheap_vol_max_pct=None, **kwargs))
        # cheap-vol-gated entry (buy while vol is low)
        rows.append(grade_underlying(u, cheap_vol_max_pct=11.0, **kwargs))
    return {"count": len(rows), "eligible_for_paper": sum(
        1 for r in rows if (r.get("paper_gate") or {}).get("eligible_for_paper")), "rows": rows}
