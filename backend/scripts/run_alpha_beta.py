#!/usr/bin/env python3
"""ERP P5-M4: alpha-vs-beta separation for the whole book. Research-only.

Builds a daily short-vol benchmark from the bhavcopy store, pulls each strategy's
daily realized P&L from db.trade_fills, and regresses each strategy's daily returns
on (short_vol, nifty). Reports α, its t-stat, the betas and R², plus a verdict:
REPLICABLE_SHORT_VOL_BETA means the strategy is the premium-selling risk factor the
book pays costs to reproduce (the §20 "one bet" thesis, tested).

Run ON THE VPS in the backend container (needs Mongo + the populated store):
  docker exec quantg-backend python /app/scripts/run_alpha_beta.py --start 2024-01-01 --end 2026-07-31
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from core.bhavcopy_store import BhavcopyStore  # noqa: E402
from core.alpha_beta import (  # noqa: E402
    ols, classify_alpha_beta, short_vol_benchmark,
)


def _ist_day(ts: str) -> str:
    return str(ts or "")[:10]


async def _strategy_daily_returns(db: Any, equity: float) -> Dict[str, List[Dict[str, Any]]]:
    """Per-strategy [{date, ret}] from realized fill P&L, ret = day_pnl / equity."""
    by_strat: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    names: Dict[str, str] = {}
    cur = db.trade_fills.find(
        {"realized_pnl": {"$exists": True}},
        {"_id": 0, "strategy_id": 1, "strategy_name": 1, "realized_pnl": 1,
         "created_at": 1, "closed_at": 1, "filled_at": 1},
    )
    async for f in cur:
        sid = str(f.get("strategy_id") or "unknown")
        pnl = f.get("realized_pnl")
        if pnl in (None, 0):
            continue
        day = _ist_day(f.get("closed_at") or f.get("filled_at") or f.get("created_at"))
        if not day:
            continue
        by_strat[sid][day] += float(pnl)
        if f.get("strategy_name"):
            names[sid] = f["strategy_name"]
    out: Dict[str, List[Dict[str, Any]]] = {}
    for sid, daymap in by_strat.items():
        rows = [{"date": d, "ret": p / equity} for d, p in sorted(daymap.items())]
        if len(rows) >= 20:
            out[names.get(sid, sid)] = rows
    return out


async def run(start: str, end: str, underlying: str, hold_days: int, equity: float) -> Dict[str, Any]:
    mongo_url = os.environ.get("MONGO_URL") or os.environ.get("MONGO_URI") or "mongodb://mongo:27017"
    db = AsyncIOMotorClient(mongo_url)[os.environ.get("DB_NAME") or "quantg"]

    store = BhavcopyStore()
    sv = short_vol_benchmark(store, start, end, underlying=underlying, hold_days=hold_days)
    nifty_bars = store.underlying_daily(underlying, start, end)
    nifty = []
    for i in range(1, len(nifty_bars)):
        p0, p1 = nifty_bars[i - 1]["close"], nifty_bars[i]["close"]
        if p0 > 0:
            nifty.append({"date": nifty_bars[i]["date"][:10], "ret": (p1 - p0) / p0})

    sv_by = {r["date"]: r["ret"] for r in sv}
    mkt_by = {r["date"]: r["ret"] for r in nifty}

    strat_series = await _strategy_daily_returns(db, equity)
    rows: List[Dict[str, Any]] = []
    for name, series in strat_series.items():
        aligned = [(r["date"], r["ret"]) for r in series
                   if r["date"] in sv_by and r["date"] in mkt_by]
        if len(aligned) < 20:
            rows.append({"name": name, "verdict": "INSUFFICIENT_OVERLAP", "n": len(aligned)})
            continue
        y = [r for _, r in aligned]
        reg = ols(y, {"short_vol": [sv_by[d] for d, _ in aligned],
                      "nifty": [mkt_by[d] for d, _ in aligned]})
        if reg is None:
            rows.append({"name": name, "verdict": "SINGULAR", "n": len(aligned)})
            continue
        reg.verdict = classify_alpha_beta(reg)
        d = reg.as_dict()
        d["name"] = name
        rows.append(d)
    result = {"kind": "alpha_beta", "short_vol_days": len(sv), "nifty_days": len(nifty),
              "window": {"start": start, "end": end}, "underlying": underlying,
              "hold_days": hold_days, "equity": equity, "strategies": rows,
              "generated_at": datetime.now(timezone.utc).isoformat()}
    try:
        await db.alpha_beta_runs.insert_one(dict(result))
    except Exception as exc:  # noqa: BLE001
        print(f"(warn: could not persist alpha_beta run: {exc})")
    result.pop("_id", None)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-07-31")
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument("--hold-days", type=int, default=1)
    ap.add_argument("--equity", type=float, default=500000.0)
    args = ap.parse_args()
    res = asyncio.run(run(args.start, args.end, args.underlying, args.hold_days, args.equity))
    print("ERP P5-M4 — alpha vs short-vol/market beta")
    print(f"short-vol benchmark days: {res['short_vol_days']}, nifty days: {res['nifty_days']}\n")
    print(f"{'STRATEGY':<40}{'N':>5}{'ALPHA':>10}{'t(A)':>7}{'B_sv':>7}{'B_nif':>7}{'R2':>7}  VERDICT")
    print("-" * 100)
    for r in sorted(res["strategies"], key=lambda x: x.get("alpha", 0), reverse=True):
        if "alpha" not in r:
            print(f"{r['name'][:39]:<40}{r.get('n', 0):>5}  {r['verdict']}")
            continue
        b = r["betas"]
        print(f"{r['name'][:39]:<40}{r['n']:>5}{r['alpha']:>10.5f}"
              f"{(r['alpha_t'] or 0):>7.2f}{b.get('short_vol', 0):>7.2f}"
              f"{b.get('nifty', 0):>7.2f}{r['r_squared']:>7.3f}  {r['verdict']}")


if __name__ == "__main__":
    main()
