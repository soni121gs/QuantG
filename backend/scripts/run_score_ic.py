#!/usr/bin/env python3
"""ERP P5-M5: information-coefficient screen. Research-only, read-only.

Joins each stored pre-trade score with the realized forward P&L of the trade it
scored (from closed db.strategy_positions) and reports the Spearman IC. A
DECORATION verdict means the score has no predictive content — the machinery that
computes it is not earning its place, and any sizing leaning on it is sizing on
noise.

Scores checked:
  * contract_edge_score            (core/dynamic_contract_selector, §16.4)
  * regime_fine_confidence_at_entry(RAE regime classifier, §18)
  * edge_math.conviction           (core/edge_sizer, §16)

Run ON THE VPS in the backend container:
  docker exec quantg-backend python /app/scripts/run_score_ic.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from core.score_ic import information_coefficient  # noqa: E402


def _get(doc: Dict[str, Any], path: str):
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


async def run(min_pairs: int) -> Dict[str, Any]:
    mongo_url = os.environ.get("MONGO_URL") or os.environ.get("MONGO_URI") or "mongodb://mongo:27017"
    db = AsyncIOMotorClient(mongo_url)[os.environ.get("DB_NAME") or "quantg"]

    score_paths = {
        "contract_edge_score": "contract_edge_score",
        "regime_confidence": "regime_fine_confidence_at_entry",
        "edgemath_conviction": "edge_math.conviction",
    }
    pairs: Dict[str, List[Tuple[float, float]]] = {k: [] for k in score_paths}
    cur = db.strategy_positions.find(
        {"status": "CLOSED", "realized_pnl": {"$exists": True}},
        {"_id": 0, "realized_pnl": 1, "contract_edge_score": 1,
         "regime_fine_confidence_at_entry": 1, "edge_math": 1},
    )
    n_docs = 0
    async for p in cur:
        pnl = p.get("realized_pnl")
        if pnl is None:
            continue
        n_docs += 1
        for name, path in score_paths.items():
            s = _get(p, path)
            if s is not None:
                try:
                    pairs[name].append((float(s), float(pnl)))
                except (TypeError, ValueError):
                    pass
    results = [information_coefficient(name, ps).as_dict() for name, ps in pairs.items()]
    out = {"kind": "score_ic", "closed_positions_scanned": n_docs, "results": results,
           "generated_at": datetime.now(timezone.utc).isoformat()}
    try:
        await db.score_ic_runs.insert_one(dict(out))
    except Exception as exc:  # noqa: BLE001
        print(f"(warn: could not persist score_ic run: {exc})")
    out.pop("_id", None)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-pairs", type=int, default=20)
    args = ap.parse_args()
    res = asyncio.run(run(args.min_pairs))
    print("ERP P5-M5 — information coefficient of stored scores vs realized P&L")
    print(f"closed positions scanned: {res['closed_positions_scanned']}\n")
    print(f"{'SCORE':<28}{'N':>6}{'IC':>9}{'t':>8}  VERDICT")
    print("-" * 62)
    for r in res["results"]:
        ic = "-" if r["ic"] is None else f"{r['ic']:.4f}"
        t = "-" if r["t_stat"] is None else f"{r['t_stat']:.2f}"
        print(f"{r['name']:<28}{r['n']:>6}{ic:>9}{t:>8}  {r['verdict']}")


if __name__ == "__main__":
    main()
