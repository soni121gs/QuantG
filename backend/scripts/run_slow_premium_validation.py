#!/usr/bin/env python3
"""ERP P3-3 slow premium core restore validation. Research-only."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.slow_premium_core import grade_slow_premium  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--min-z", type=float, default=0.75)
    ap.add_argument("--entry-stride-days", type=int, default=5)
    args = ap.parse_args()
    result = grade_slow_premium(
        start=args.start, end=args.end, min_z=args.min_z, entry_stride_days=args.entry_stride_days,
    )
    print("ERP P3-3 S4 Slow premium core restore validation")
    print("Rules: QG-O1 held rich put-spread + SENSEX monthly defined-risk strangle; IV-richness gated")
    print(f"Rows: {result['count']}; eligible_for_paper={result['eligible_for_paper']}\n")
    print(f"{'NAME':<42}{'SIG':>6}{'N':>5}{'EXP':>9}{'OOS':>9}{'DSR':>8}  VERDICT  PAPER")
    print("-" * 104)
    for row in result["rows"]:
        if row.get("status") == "error":
            print(f"{row.get('name','?')[:41]:<42}{'-':>6}{'-':>5}{'-':>9}{'-':>9}{'-':>8}  ERROR {row.get('error')}")
            continue
        gate = row.get("paper_gate") or {}
        dsr = gate.get("deflated_sharpe") or {}
        overall = row.get("overall") or {}
        oos = row.get("oos") or {}
        print(
            f"{row['name'][:41]:<42}{(row.get('selector') or {}).get('accepted', 0):>6}"
            f"{overall.get('n', 0):>5}{overall.get('expectancy', 0):>9.1f}{oos.get('expectancy', 0):>9.1f}"
            f"{dsr.get('deflated_sharpe', 0):>8.2f}  {row.get('verdict')}  "
            f"{'YES' if gate.get('eligible_for_paper') else 'NO'}"
        )


if __name__ == "__main__":
    main()
