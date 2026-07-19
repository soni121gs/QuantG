#!/usr/bin/env python3
"""ERP P3-2 daily delta-1 momentum validation. Research-only."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.daily_delta1_momentum import DEFAULT_UNDERLYINGS, grade_universe  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--underlyings", default=",".join(DEFAULT_UNDERLYINGS))
    args = ap.parse_args()
    underlyings = [u.strip().upper() for u in args.underlyings.split(",") if u.strip()]
    result = grade_universe(underlyings, start=args.start, end=args.end)
    print("ERP P3-2 S3 Daily delta-1 momentum validation")
    print("Rules: 20-day time-series momentum + separate overnight variant; deep-ITM single-leg; vol-target lots reported")
    print(f"Rows: {result['count']}; eligible_for_paper={result['eligible_for_paper']}\n")
    print(f"{'UNDERLYING':<12}{'VARIANT':<16}{'SIG':>6}{'N':>5}{'EXP':>9}{'OOS':>9}{'DSR':>8}{'REG':>5}  VERDICT  PAPER")
    print("-" * 110)
    for row in result["rows"]:
        if row.get("status") == "error":
            print(f"{row['underlying']:<12}{row.get('variant','-'):<16}{'-':>6}{'-':>5}{'-':>9}{'-':>9}{'-':>8}{'-':>5}  ERROR {row.get('error')}")
            continue
        gate = row.get("paper_gate") or {}
        dsr = gate.get("deflated_sharpe") or {}
        reg = gate.get("regime_gate") or {}
        overall = row.get("overall") or {}
        oos = row.get("oos") or {}
        print(
            f"{row['underlying']:<12}{row['variant']:<16}{(row.get('selector') or {}).get('accepted', 0):>6}"
            f"{overall.get('n', 0):>5}{overall.get('expectancy', 0):>9.1f}{oos.get('expectancy', 0):>9.1f}"
            f"{dsr.get('deflated_sharpe', 0):>8.2f}{reg.get('regime_count', 0):>5}  {row.get('verdict')}  "
            f"{'YES' if gate.get('eligible_for_paper') else 'NO'}"
        )


if __name__ == "__main__":
    main()
