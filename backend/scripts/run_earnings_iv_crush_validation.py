#!/usr/bin/env python3
"""ERP P3-1 earnings IV-crush validation.

Research-only: builds T-1 earnings signals, exits T+1, skips expiry week, and
prints the OOS/paper gates. It never seeds or wakes a strategy.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.earnings_iv_crush import TOP30_FNO, grade_universe  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--symbols", default=",".join(TOP30_FNO))
    ap.add_argument("--short-otm-pct", type=float, default=0.08)
    ap.add_argument("--wing-width", type=int, default=2)
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    result = grade_universe(
        symbols,
        start=args.start,
        end=args.end,
        short_otm_pct=args.short_otm_pct,
        wing_width=args.wing_width,
    )
    print("ERP P3-1 S1 Earnings IV-crush premium validation")
    print("Rules: defined-risk iron condor, entry T-1, exit T+1, skip event==expiry/expiry week")
    print(f"Universe: {result['count']} symbols; eligible_for_paper={result['eligible_for_paper']}\n")
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
            f"{row['symbol']:<12}{sel.get('events', 0):>7}{sel.get('accepted', 0):>6}"
            f"{overall.get('n', 0):>5}{overall.get('expectancy', 0):>9.1f}"
            f"{oos.get('expectancy', 0):>9.1f}{dsr.get('deflated_sharpe', 0):>8.2f}"
            f"{floor.get('multiple') or 0:>7.2f}  {row.get('verdict')}  "
            f"{'YES' if gate.get('eligible_for_paper') else 'NO'}"
        )


if __name__ == "__main__":
    main()
