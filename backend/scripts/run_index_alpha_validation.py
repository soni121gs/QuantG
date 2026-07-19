#!/usr/bin/env python3
"""ERP index-option alpha sleeve validation (NIFTY + SENSEX). Research-only.

Grades the selective-VRP + trend-coverage index sleeves on the real bhavcopy
store (VPS: /app/data/bhavcopy_fo). Prints signal count, sample, expectancy,
OOS expectancy, deflated Sharpe, cost-floor multiple, verdict, and the hard
paper gate. Nothing here trades, wakes a registry row, or touches the broker.

    docker exec quantg-backend python /app/scripts/run_index_alpha_validation.py \
        --start 2019-01-01 --end 2026-07-18 --min-z 0.75
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.index_alpha_sleeves import grade_index_alpha  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--min-z", type=float, default=0.75,
                    help="IV richness z-gate: sellers need z>=min_z (rich), trend needs z<=-min_z (cheap)")
    args = ap.parse_args()

    result = grade_index_alpha(start=args.start, end=args.end, min_z=args.min_z)
    print("ERP Index-Option Alpha Sleeves — NIFTY + SENSEX (research-only)")
    print("Gate: selective VRP (RANGE + IV-rich) + trend-coverage delta-1 (TREND + IV-cheap)")
    print(f"Paper gate = CANDIDATE_EDGE AND deflated-Sharpe pass AND cost-floor >= 3x friction")
    print(f"Rows: {result['count']}; eligible_for_paper={result['eligible_for_paper']}\n")
    print(f"{'NAME':<46}{'SIG':>5}{'N':>5}{'EXP':>8}{'OOS':>8}{'DSR':>7}{'CFx':>6}  VERDICT  PAPER")
    print("-" * 108)
    for row in result["rows"]:
        if row.get("status") == "error":
            print(f"{row.get('name','?')[:45]:<46}{'-':>5}{'-':>5}{'-':>8}{'-':>8}{'-':>7}{'-':>6}"
                  f"  ERROR  {row.get('error')}")
            continue
        gate = row.get("paper_gate") or {}
        dsr = gate.get("deflated_sharpe") or {}
        cf = gate.get("cost_floor") or {}
        overall = row.get("overall") or {}
        oos = row.get("oos") or {}
        print(
            f"{row['name'][:45]:<46}{(row.get('selector') or {}).get('accepted', 0):>5}"
            f"{overall.get('n', 0):>5}{overall.get('expectancy', 0):>8.0f}"
            f"{oos.get('expectancy', 0):>8.0f}{dsr.get('deflated_sharpe', 0):>7.2f}"
            f"{(cf.get('mult') or 0):>6.1f}  {row.get('verdict')}  "
            f"{'YES' if gate.get('eligible_for_paper') else 'NO'}"
        )


if __name__ == "__main__":
    main()
