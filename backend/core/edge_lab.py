"""Edge Lab snapshot builder — precomputes the OOS research surface for the UI.

Aggregates, from the free bhavcopy store (core/bhavcopy_store), the four things
the Edge Lab tab needs to tell the honest truth about the book:

  - coverage : what REAL data we actually have (window, underlyings, gaps)
  - base_rate: the short-OTM-vol finding (the vol risk premium by OTM distance)
               + the directional base rate (there is no trend edge)
  - oos      : per-strategy walk-forward verdict — the truth about the live book
  - sweep    : the credit-spread exit-geometry grid → does ANY config cross +OOS?

Pure, synchronous compute so it can be driven from a CLI (pymongo) OR from an
endpoint (Motor + asyncio.to_thread). It is deliberately heavy (it prices two
years of option chains many times over), so it is meant to be run OCCASIONALLY
and cached in db.edge_lab_snapshots, never on the request path. See
routes/ops.py (GET /ops/edge-lab, POST /ops/edge-lab/refresh) and
scripts/build_edge_lab_snapshot.py.

Mirrors the standalone research scripts (base_rate_studies.py / run_edge_sweep.py)
so the UI shows exactly what those console tools show — this is their JSON home.
"""
from __future__ import annotations

import itertools
import os
import statistics as st
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.bhavcopy_store import BhavcopyStore
from core.eod_options_backtest import EODOptionsBacktest, walk_forward

# underlyings we probe for coverage vs. the smaller set the studies run on
COVERAGE_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
BASE_RATE_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"]
LOT = {"NIFTY": 75, "BANKNIFTY": 35, "SENSEX": 20, "BANKEX": 15, "FINNIFTY": 65, "MIDCPNIFTY": 120}

_ENTRY_DTE = (2, 8)
_SLIP = float(os.environ.get("STUDY_SLIP_PCT", "0.015"))  # entry slippage per leg (matches base_rate_studies)

# sweep grid — the meaningful TP×SL exit-geometry matrix (width/DTE held fixed to
# keep the routine snapshot cheap; the full 4-axis grid lives in run_edge_sweep.py).
_CREDIT_TP = [0.35, 0.5, 0.7]
_CREDIT_SL = [1.0, 2.0, 3.5]
_WIDTH = [2]
_MAX_DTE = [10]
_MIN_DTE = 2


def _dte(a: str, b: str) -> int:
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


# ---- base rate: short OTM vol held to expiry (the vol risk premium) ----------

def _short_vol_cycles(store: BhavcopyStore, u: str, otm_pct: float) -> List[Dict[str, float]]:
    """One short strangle (otm_pct>0) / straddle (0) per weekly expiry, held to
    cash settlement. Entry slippage only; expiry is exact intrinsic. Points × lot."""
    candles = store.underlying_daily(u)
    if len(candles) < 60:
        return []
    close_by = {c["date"][:10]: c["close"] for c in candles}
    days = [c["date"][:10] for c in candles]
    lot = LOT.get(u, 50)
    out: List[Dict[str, float]] = []
    entered: set = set()
    for day in days:
        pick = None
        for e in sorted(store.expiries(u, day)):
            if _ENTRY_DTE[0] <= _dte(day, e) <= _ENTRY_DTE[1]:
                pick = e
                break
        if not pick or pick in entered:
            continue
        chain = store.option_chain(u, day).get(pick, {})
        strikes = sorted(chain.keys())
        if len(strikes) < 5:
            continue
        spot = close_by[day]
        ce_k = min(strikes, key=lambda k: abs(k - spot * (1 + otm_pct)))
        pe_k = min(strikes, key=lambda k: abs(k - spot * (1 - otm_pct)))
        ce = store.leg_settle(u, day, pick, ce_k, "CE")
        pe = store.leg_settle(u, day, pick, pe_k, "PE")
        if not ce or not pe:
            continue
        credit = (ce + pe) * (1 - _SLIP)
        s_exp = close_by.get(pick)
        if s_exp is None:                       # expiry not a stored day -> nearest prior
            prior = [d for d in days if d <= pick]
            if not prior:
                continue
            s_exp = close_by[prior[-1]]
        payoff = max(0.0, s_exp - ce_k) + max(0.0, pe_k - s_exp)
        pnl_pts = credit - payoff               # short: keep credit, pay intrinsic
        entered.add(pick)
        out.append({"pnl_pts": pnl_pts, "rupees": pnl_pts * lot})
    return out


def _summ(rows: List[Dict[str, float]]) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    pnls = [r["pnl_pts"] for r in rows]
    rup = [r["rupees"] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": len(rows),
        "mean_pts": round(st.mean(pnls), 1),
        "win_rate": round(100 * wins / len(rows), 1),
        "mean_rupees": round(st.mean(rup), 0),
        "sum_rupees": round(sum(rup), 0),
        "worst_pts": round(min(pnls), 0),
    }


def _base_rate(store: BhavcopyStore) -> List[Dict[str, Any]]:
    out = []
    for u in BASE_RATE_UNDERLYINGS:
        row: Dict[str, Any] = {"underlying": u, "lot": LOT.get(u)}
        for label, otm in (("atm", 0.0), ("otm_1pct", 0.01), ("otm_2pct", 0.02)):
            row[label] = _summ(_short_vol_cycles(store, u, otm))
        out.append(row)
    return out


def _directional(store: BhavcopyStore) -> List[Dict[str, Any]]:
    """Does 20-day momentum predict the next 5 days? ~50% WR / ~0% mean = random walk."""
    out = []
    for u in BASE_RATE_UNDERLYINGS:
        closes = [c["close"] for c in store.underlying_daily(u)]
        cont = tot = 0
        fwd: List[float] = []
        for i in range(20, len(closes) - 5):
            up = closes[i] > closes[i - 20]
            f = (closes[i + 5] - closes[i]) / closes[i]
            fwd.append(f if up else -f)
            tot += 1
            if (f > 0) == up:
                cont += 1
        if tot:
            out.append({"underlying": u, "continuation_wr": round(100 * cont / tot, 0),
                        "mean_fwd_return_pct": round(100 * st.mean(fwd), 2), "n": tot})
    return out


# ---- coverage ----------------------------------------------------------------

def _coverage(store: BhavcopyStore, days: List[str]) -> Dict[str, Any]:
    unders = []
    for u in COVERAGE_UNDERLYINGS:
        ud = store.underlying_daily(u)
        if ud:
            unders.append({"underlying": u, "days": len(ud),
                           "first": ud[0]["date"][:10], "last": ud[-1]["date"][:10]})
    return {
        "n_days": len(days),
        "first": days[0] if days else None,
        "last": days[-1] if days else None,
        "underlyings": unders,
        "gaps": [
            "Equity (NSE_EQ stock EOD) not ingested — index derivatives only",
            "Intraday / 1-min data absent — validates held-to-theta, not scalpers",
        ],
    }


# ---- per-strategy OOS verdict ------------------------------------------------

def _oos(strategies: List[Dict[str, Any]], store: BhavcopyStore) -> Dict[str, Any]:
    engine = EODOptionsBacktest(store)
    rows = []
    for s in strategies:
        res = engine.run(s)
        if res.get("error"):
            rows.append({"name": s.get("name"), "error": res["error"]})
            continue
        wf = walk_forward(res)
        o = wf["overall"]
        rows.append({
            "name": res.get("name"), "underlying": res.get("underlying"),
            "structure": res.get("structure"), "verdict": wf["verdict"],
            "n": o["n"], "expectancy": o["expectancy"], "win_rate": o["win_rate"],
            "pnl": o["pnl"], "oos_year": wf.get("oos_year"),
            "oos_expectancy": wf.get("oos", {}).get("expectancy", 0),
            "pct_green_months": wf.get("pct_green_months"),
        })
    order = {"CANDIDATE_EDGE": 0, "FRAGILE": 1, "INSUFFICIENT_DATA": 2, "NO_EDGE_NEGATIVE": 3}
    rows.sort(key=lambda r: order.get(r.get("verdict", ""), 9))
    counts: Dict[str, int] = {}
    for r in rows:
        v = r.get("verdict") or ("ERROR" if r.get("error") else "?")
        counts[v] = counts.get(v, 0) + 1
    return {"rows": rows, "counts": counts}


# ---- exit-geometry sweep -----------------------------------------------------

def _sweep(strategies: List[Dict[str, Any]], store: BhavcopyStore) -> List[Dict[str, Any]]:
    engine = EODOptionsBacktest(store)
    targets = [s for s in strategies
               if "theta credit spread" in (s.get("name") or "").lower()
               and any(u in (s.get("name") or "").upper() for u in ("NIFTY", "BANKNIFTY"))]
    if not targets:  # fallback: any credit-spread strategies
        targets = [s for s in strategies
                   if (((s.get("visual_config") or {}).get("options") or {}).get("structure")
                       == "credit_spread")][:2]
    grid = list(itertools.product(_CREDIT_TP, _CREDIT_SL, _WIDTH, _MAX_DTE))
    out = []
    for strat in targets:
        cells = []
        pos_oos = edges = 0
        for tp, sl, width, dte in grid:
            params = {"credit_tp": tp, "credit_sl": sl, "width": width,
                      "min_dte": _MIN_DTE, "max_dte": dte, "max_hold_days": dte}
            res = engine.run(strat, params=params)
            if res.get("error"):
                continue
            wf = walk_forward(res)
            o = wf["overall"]
            oe = wf["oos"].get("expectancy", 0)
            if oe > 0:
                pos_oos += 1
            if wf["verdict"] == "CANDIDATE_EDGE":
                edges += 1
            cells.append({"tp": tp, "sl": sl, "width": width, "dte": dte, "n": o["n"],
                          "expectancy": o["expectancy"], "oos_expectancy": oe,
                          "win_rate": o["win_rate"], "verdict": wf["verdict"]})
        cells.sort(key=lambda c: -c["oos_expectancy"])
        out.append({"name": strat.get("name"), "configs": len(cells),
                    "positive_oos": pos_oos, "candidate_edges": edges, "cells": cells})
    return out


# ---- top-level ---------------------------------------------------------------

def build_snapshot(strategies: List[Dict[str, Any]], store: Optional[BhavcopyStore] = None,
                   include_sweep: bool = True) -> Dict[str, Any]:
    """Compute the full Edge Lab snapshot. Returns a JSON-serialisable dict with a
    top-level `status` ('ready' or 'empty') and `generated_at` (UTC ISO)."""
    import time
    store = store or BhavcopyStore()
    days = store.trading_days()
    now = datetime.now(timezone.utc).isoformat()
    if not days:
        return {"status": "empty", "generated_at": now,
                "hint": "bhavcopy store empty — run scripts/bhavcopy_ingest.py and mount /app/data/bhavcopy_fo"}

    def _phase(label, fn):
        t0 = time.time()
        print(f"[edge_lab] {label}…", flush=True)
        out = fn()
        print(f"[edge_lab] {label} done in {time.time() - t0:.0f}s", flush=True)
        return out

    coverage = _phase("coverage", lambda: _coverage(store, days))
    base_rate = _phase("base_rate", lambda: {
        "short_vol": _base_rate(store), "directional": _directional(store), "slippage_pct": _SLIP})
    oos = _phase("oos", lambda: _oos(strategies, store))
    sweep = _phase("sweep", lambda: _sweep(strategies, store)) if include_sweep else None
    return {"status": "ready", "generated_at": now, "coverage": coverage,
            "base_rate": base_rate, "oos": oos, "sweep": sweep}
