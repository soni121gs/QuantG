"""RAE-2 (2026-07-10): the REFORMED judge — regime-conditional walk-forward OOS.

The old OOS engines grade a strategy on a BLENDED all-days number that averages its
good regime with its bad one. That is why a real regime-specialist looks dead (a
range-seller's edge is drowned by its trend-day losses) and why the founder
distrusts the verdict. This judge fixes that with three rules (CLAUDE.md §18.2 #6):

  1. Grade a specialist ONLY on the days of the regime it OWNS. Off-regime days are
     reported separately as "give-back" (a good specialist stands down → ~0 there).
  2. Walk-forward WITHIN the regime — in-sample = all but the latest year, OOS = the
     latest year, both measured on regime days only.
  3. A thin sample is `NEEDS_FORWARD_PAPER`, NEVER an auto-veto. Regime days (esp.
     TREND ~1%) are rare; refusing to judge is honest, killing on n<threshold is not.

Pure, no I/O. Input is a per-day record stream any backtest can produce:
    records = [{"date": "YYYY-MM-DD", "regime": <label>, "pnl": <float|None>}, ...]
`pnl=None` means the specialist STOOD DOWN that day (it counts toward stand-down
discipline, not toward P&L). `regime` is the day's canonical label (RAE-0).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# regime days are rare; the bar is deliberately modest, and below it we ask for
# forward-paper rather than vetoing.
JUDGE_MIN_TRADES = _i("RAE_JUDGE_MIN_TRADES", 20)

POSITIVE_OOS = "POSITIVE_OOS"
NEEDS_FORWARD_PAPER = "NEEDS_FORWARD_PAPER"
NO_EDGE = "NO_EDGE"
INSUFFICIENT = "INSUFFICIENT_DATA"


def _stats(pnls: Sequence[float]) -> Dict[str, float]:
    n = len(pnls)
    if n == 0:
        return {"n": 0, "total": 0.0, "avg": 0.0, "wr": 0.0}
    wins = [p for p in pnls if p > 0]
    return {"n": n, "total": round(sum(pnls), 1), "avg": round(sum(pnls) / n, 1),
            "wr": round(len(wins) / n * 100, 1)}


@dataclass
class RegimeVerdict:
    target_regime: str
    verdict: str
    on_regime: Dict[str, float] = field(default_factory=dict)       # traded days in-regime
    in_sample: Dict[str, float] = field(default_factory=dict)       # earlier years, in-regime
    out_sample: Dict[str, float] = field(default_factory=dict)      # latest year, in-regime
    off_regime: Dict[str, float] = field(default_factory=dict)      # traded days OUT of regime (give-back)
    stood_down_days: int = 0
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"target_regime": self.target_regime, "verdict": self.verdict,
                "on_regime": self.on_regime, "in_sample": self.in_sample,
                "out_sample": self.out_sample, "off_regime": self.off_regime,
                "stood_down_days": self.stood_down_days, "reasons": self.reasons}


def evaluate_regime_conditional(
    records: Sequence[Dict[str, Any]],
    target_regime: str,
    *,
    min_trades: Optional[int] = None,
) -> RegimeVerdict:
    """Judge a specialist that OWNS `target_regime`. See module docstring for the
    record shape."""
    min_trades = JUDGE_MIN_TRADES if min_trades is None else min_trades
    reasons: List[str] = []

    on_regime_pnls: List[float] = []
    off_regime_pnls: List[float] = []
    stood_down = 0
    by_year_in_regime: Dict[str, List[float]] = {}

    for r in records:
        pnl = r.get("pnl")
        in_regime = str(r.get("regime")) == target_regime
        if pnl is None:
            if in_regime:
                stood_down += 1   # stood down ON its own regime day (recall gap)
            continue
        if in_regime:
            on_regime_pnls.append(float(pnl))
            yr = str(r.get("date"))[:4]
            by_year_in_regime.setdefault(yr, []).append(float(pnl))
        else:
            off_regime_pnls.append(float(pnl))

    on = _stats(on_regime_pnls)
    off = _stats(off_regime_pnls)

    years = sorted(by_year_in_regime)
    in_s = _stats(sum([by_year_in_regime[y] for y in years[:-1]], [])) if len(years) >= 2 else _stats([])
    oos = _stats(by_year_in_regime[years[-1]]) if years else _stats([])

    # verdict
    if on["n"] == 0:
        verdict = INSUFFICIENT
        reasons.append("specialist never traded on its target regime")
    elif on["avg"] <= 0:
        verdict = NO_EDGE
        reasons.append(f"on-regime avg {on['avg']} <= 0 across {on['n']} days")
    elif on["n"] < min_trades or len(years) < 2:
        verdict = NEEDS_FORWARD_PAPER
        reasons.append(f"positive (avg {on['avg']}) but thin: n={on['n']} (<{min_trades}) "
                       f"or single-year ({len(years)}) — forward-paper, do NOT veto")
    elif in_s["avg"] > 0 and oos["avg"] > 0:
        verdict = POSITIVE_OOS
        reasons.append(f"in-sample avg {in_s['avg']} AND OOS avg {oos['avg']} both > 0 on regime days")
    else:
        verdict = NEEDS_FORWARD_PAPER
        reasons.append(f"one walk-forward slice non-positive (IS {in_s['avg']} / OOS {oos['avg']}) — forward-paper")

    if off["n"] and off["avg"] < -1.0:
        reasons.append(f"WARNING: gives back avg {off['avg']} on {off['n']} OFF-regime days — "
                       f"tighten the stand-down gate (it should not trade off-regime)")

    return RegimeVerdict(
        target_regime=target_regime, verdict=verdict,
        on_regime=on, in_sample=in_s, out_sample=oos, off_regime=off,
        stood_down_days=stood_down, reasons=reasons,
    )
