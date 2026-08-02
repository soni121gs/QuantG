#!/usr/bin/env python3
"""ERP P3-1 earnings IV-crush validation.

Research-only: builds T-1 earnings signals, exits T+1, skips expiry week, and
prints the OOS/paper gates. It never seeds or wakes a strategy.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.earnings_iv_crush import TOP30_FNO, grade_universe  # noqa: E402


def _num(x: Any, default: float = 0.0) -> float:
    """Coerce a possibly-None/str metric to a float for :>.Nf formatting.
    `dict.get(k, default)` returns None when the key exists but is None, which
    crashed the row printer — this is the guard."""
    try:
        return float(x) if x is not None else float(default)
    except (TypeError, ValueError):
        return float(default)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--symbols", default=None,
                    help="comma list; default = full F&O options universe from the store (P5-M2)")
    ap.add_argument("--top30", action="store_true",
                    help="use only the hand-maintained top-30 names instead of the full store universe")
    ap.add_argument("--short-otm-pct", type=float, default=0.08)
    ap.add_argument("--wing-width", type=int, default=2)
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.top30:
        symbols = list(TOP30_FNO)
    else:
        # P5-M2 breadth: every stock that actually has options in the store, so the
        # n>=300-event paper gate is reachable (30 names cannot clear it arithmetically).
        from scripts.earnings_calendar_fetch_nse import fno_universe_from_store
        symbols = fno_universe_from_store(lookback_days=60)
    result = grade_universe(
        symbols,
        start=args.start,
        end=args.end,
        short_otm_pct=args.short_otm_pct,
        wing_width=args.wing_width,
    )
    print("ERP P3-1 S1 Earnings IV-crush premium validation")
    print("Rules: defined-risk iron condor, entry T-1, exit T+1, skip event==expiry/expiry week")
    print(f"Universe: {result['count']} symbols; per-symbol eligible={result['eligible_for_paper']}\n")

    # The REAL test — the sleeve is ONE breadth strategy pooling all names' events.
    pooled = result.get("pooled") or {}
    print("=== POOLED (the flagship's actual gate — all names as one strategy) ===")
    print(f"  pooled trades n={int(_num(pooled.get('n')))}  verdict={pooled.get('verdict')}  "
          f"expectancy={_num(pooled.get('expectancy')):.1f}  "
          f"t_HAC={_num(pooled.get('t_stat_hac')):.2f}")
    _dsr = pooled.get("deflated_sharpe") or {}
    _fl = pooled.get("cost_floor") or {}
    print(f"  DSR passed={_dsr.get('passed')}  cost_floor x={_num(_fl.get('multiple')):.2f} "
          f"passed={_fl.get('passed')}  sample>=300={ (pooled.get('sample_gate') or {}).get('passed')}")
    print(f"  ELIGIBLE_FOR_PAPER={pooled.get('eligible_for_paper')}\n")
    print(f"{'SYMBOL':<12}{'EVENTS':>7}{'SIG':>6}{'N':>5}{'EXP':>9}{'OOS':>9}{'DSR':>8}{'COSTx':>7}  VERDICT  PAPER")
    print("-" * 104)
    for row in result["rows"]:
        if row.get("status") == "error":
            print(f"{row['symbol']:<12}{'-':>7}{'-':>6}{'-':>5}{'-':>9}{'-':>9}{'-':>8}{'-':>7}  ERROR {row.get('error')}")
            continue
        sel = row.get("selector") or {}
        overall = row.get("overall") or {}
        gate = row.get("paper_gate") or {}
        dsr = gate.get("deflated_sharpe") or {}
        floor = gate.get("cost_floor") or {}
        oos = row.get("oos") or {}
        print(
            f"{row['symbol']:<12}{int(_num(sel.get('events'))):>7}{int(_num(sel.get('accepted'))):>6}"
            f"{int(_num(overall.get('n'))):>5}{_num(overall.get('expectancy')):>9.1f}"
            f"{_num(oos.get('expectancy')):>9.1f}{_num(dsr.get('deflated_sharpe')):>8.2f}"
            f"{_num(floor.get('multiple')):>7.2f}  {row.get('verdict')}  "
            f"{'YES' if gate.get('eligible_for_paper') else 'NO'}"
        )


if __name__ == "__main__":
    main()
