#!/usr/bin/env python3
"""Run ERP Phase 3 remaining validators (P3-4..P3-7). Research-only."""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.earnings_iv_crush import TOP30_FNO  # noqa: E402
from core.phase3_remaining import (  # noqa: E402
    assemble_book,
    grade_participant_oi_overlay,
    grade_pead,
    rejudge_paused_sellers,
)


def _print_pead(res):
    o = res["overall"]
    print(f"P3-4 PEAD: n={o['n']} exp={o['expectancy']} wr={o['win_rate']} verdict={res['verdict']} paper={res['paper_gate']['eligible_for_paper']}")


def _print_oi(res):
    o = res["overall"]
    print(f"P3-5 participant-OI: n={o['n']} exp={o['expectancy']} wr={o['win_rate']} verdict={res['verdict']} wire={res['overlay_gate']['wire_overlay']}")


def _print_sellers(res):
    print("P3-6 paused-seller re-judge:")
    for row in res["rows"]:
        base = row.get("base") or {}
        print(f"  {row['name']}: n={base.get('n', 0)} exp={base.get('expectancy', 0)} action={row['founder_action']}")


def _print_book(res):
    p = res["portfolio"]
    print(f"P3-7 book assembly: candidates={p['candidate_count']} heat={p['heat_score']} ready={p['paper_book_ready']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=["all", "p3-4", "p3-5", "p3-6", "p3-7"], default="all")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--symbols", default=",".join(TOP30_FNO[:5]))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    results = {}
    if args.task in {"all", "p3-4"}:
        results["p3-4"] = grade_pead(symbols, start=args.start, end=args.end)
    if args.task in {"all", "p3-5"}:
        results["p3-5"] = grade_participant_oi_overlay(start=args.start, end=args.end)
    if args.task in {"all", "p3-6"}:
        results["p3-6"] = rejudge_paused_sellers(start=args.start, end=args.end)
    if args.task in {"all", "p3-7"}:
        results["p3-7"] = assemble_book(start=args.start, end=args.end)
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True, default=str))
        return
    if "p3-4" in results:
        _print_pead(results["p3-4"])
    if "p3-5" in results:
        _print_oi(results["p3-5"])
    if "p3-6" in results:
        _print_sellers(results["p3-6"])
    if "p3-7" in results:
        _print_book(results["p3-7"])


if __name__ == "__main__":
    main()
