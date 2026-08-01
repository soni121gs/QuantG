#!/usr/bin/env python3
"""Long-vol / tail-protection sleeve validation. Research-only."""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.long_vol_tail import grade_universe  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--underlyings", default="NIFTY,BANKNIFTY,SENSEX")
    ap.add_argument("--otm-offset-pct", type=float, default=0.05)
    ap.add_argument("--entry-stride-days", type=int, default=5)
    args = ap.parse_args()
    unders = [u.strip().upper() for u in args.underlyings.split(",") if u.strip()]
    result = grade_universe(unders, start=args.start, end=args.end,
                            otm_offset_pct=args.otm_offset_pct,
                            entry_stride_days=args.entry_stride_days)
    print("Long-Vol / Tail-Protection sleeve validation (research-only)")
    print("Vehicle: long OTM index PUT held to expiry; graded on convexity, not standalone expectancy")
    print(f"Rows: {result['count']}; eligible_for_paper={result['eligible_for_paper']}\n")
    print(f"{'NAME':<40}{'GATE':>10}{'N':>5}{'EXP':>9}{'OOS':>9}  {'VERDICT':<20}"
          f"{'MAXPAY':>9}{'%PREM':>7}{'TAILx2':>7}  PAPER")
    print("-" * 128)
    for row in result["rows"]:
        if row.get("status") == "error":
            print(f"{row.get('underlying','?'):<40}{'-':>10}  ERROR {row.get('error')}")
            continue
        overall = row.get("overall") or {}
        oos = row.get("oos") or {}
        tm = row.get("tail_metrics") or {}
        gate = "cheap_vol" if row.get("gate") is not None else "uncond"
        paper = "YES" if (row.get("paper_gate") or {}).get("eligible_for_paper") else "NO"
        print(
            f"{row['underlying']:<40}{gate:>10}{overall.get('n', 0):>5}"
            f"{overall.get('expectancy', 0):>9.1f}{oos.get('expectancy', 0):>9.1f}  "
            f"{str(row.get('verdict')):<20}{tm.get('max_single_payoff', 0):>9.0f}"
            f"{tm.get('pct_premium_recovered', 0):>7.0f}{tm.get('tail_hits_gt_2x', 0):>7}  {paper}"
        )


if __name__ == "__main__":
    main()
