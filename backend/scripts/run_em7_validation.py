"""Run the EdgeMath dynamic-vs-flat OOS gate on the RES-8 seller stream."""
from __future__ import annotations

import argparse
import json

from core.bhavcopy_store import BhavcopyStore
from core.edge_oos import compare_dynamic_to_flat
from core.eod_options_backtest import EODOptionsBacktest
from core.res8_oos import build_signals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--underlying", default="NIFTY")
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--min-edge", type=float, default=0.0)
    args = parser.parse_args()

    store = BhavcopyStore()
    signals = build_signals(
        store, args.underlying, args.start, args.end, "seller", args.min_edge,
    )
    strategy = {
        "id": "em7",
        "name": f"{args.underlying} EdgeMath OOS",
        "visual_config": {
            "options": {"underlying": args.underlying, "structure": "credit_spread"},
        },
    }
    result = EODOptionsBacktest(store).run(
        strategy,
        args.start,
        args.end,
        params={
            "width": 1,
            "short_otm_pct": 0.03,
            "exit_mode": "expiry",
            "min_dte": 2,
            "max_dte": 7,
            "max_hold_days": 10,
        },
        signals=signals,
    )
    if result.get("error"):
        print(json.dumps({"verdict": "ERROR", "error": result["error"], "signals": len(signals)}, indent=2))
        return 2
    verdict = compare_dynamic_to_flat(
        result.get("trades") or [],
        per_lot_max_loss=5_000.0,
        warmup=10,
    )
    verdict["signals"] = len(signals)
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
