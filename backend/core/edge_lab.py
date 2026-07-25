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
import json
import os
import statistics as st
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.bhavcopy_store import BhavcopyStore
from core.eod_options_backtest import EODOptionsBacktest, walk_forward
from core.iv_surface import richness_zscore
from core.judge_facade import grade as judge_grade
from core.market_domains import contract_spec_for_underlying

# underlyings we probe for coverage vs. the smaller set the studies run on
COVERAGE_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
BASE_RATE_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"]

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


def _lot_size(underlying: str) -> int:
    try:
        return int(contract_spec_for_underlying(str(underlying or "").upper()).get("lot_size") or 50)
    except Exception:  # noqa: BLE001
        return 50


_OTM_LEVELS = (("atm", 0.0), ("otm_1pct", 0.01), ("otm_2pct", 0.02))


# ---- base rate: short OTM vol held to expiry (the vol risk premium) ----------

def _short_vol_multi(store: BhavcopyStore, u: str) -> Dict[str, List[Dict[str, float]]]:
    """All OTM levels in ONE pass over the days (each day's chain is built once):
    a short strangle per weekly expiry, held to cash settlement. Entry slippage
    only; expiry is exact intrinsic. Points × lot."""
    buckets: Dict[str, List[Dict[str, float]]] = {k: [] for k, _ in _OTM_LEVELS}
    candles = store.underlying_daily(u)
    if len(candles) < 60:
        return buckets
    close_by = {c["date"][:10]: c["close"] for c in candles}
    days = [c["date"][:10] for c in candles]
    lot = _lot_size(u)
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
        s_exp = close_by.get(pick)
        if s_exp is None:                       # expiry not a stored day -> nearest prior
            prior = [d for d in days if d <= pick]
            if not prior:
                continue
            s_exp = close_by[prior[-1]]
        entered.add(pick)
        for label, otm in _OTM_LEVELS:
            ce_k = min(strikes, key=lambda k: abs(k - spot * (1 + otm)))
            pe_k = min(strikes, key=lambda k: abs(k - spot * (1 - otm)))
            ce = store.leg_settle(u, day, pick, ce_k, "CE")
            pe = store.leg_settle(u, day, pick, pe_k, "PE")
            if not ce or not pe:
                continue
            credit = (ce + pe) * (1 - _SLIP)
            payoff = max(0.0, s_exp - ce_k) + max(0.0, pe_k - s_exp)
            pnl_pts = credit - payoff           # short: keep credit, pay intrinsic
            buckets[label].append({"pnl_pts": pnl_pts, "rupees": pnl_pts * lot})
    return buckets


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
        buckets = _short_vol_multi(store, u)
        row: Dict[str, Any] = {"underlying": u, "lot": _lot_size(u)}
        for label, _ in _OTM_LEVELS:
            row[label] = _summ(buckets[label])
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
    """ONE pass over the store, not N.

    The old version called store.underlying_daily(u) per index — and that helper
    re-loads EVERY day-file each time, so 6 underlyings x ~1860 days meant ~11k gz
    decompressions (the 423s stage, far worse now the store carries the full stock
    universe). One pass collects every underlying at once.

    It also reports the STOCK universe. The previous hard-coded index list made the
    183-220 backfilled stock names invisible, and `present` (derived from it) made
    the OOS gate reject any stock strategy as "no bhavcopy data" even though the
    data was right there.
    """
    idx: Dict[str, Dict[str, Any]] = {}
    stock_names: set[str] = set()
    stock_days = 0
    for d in days:
        seen_idx: set[str] = set()
        day_has_stock = False
        for r in store.load_day(d):
            it = r.get("instr_type")
            u = r.get("underlying")
            if not u:
                continue
            if it in ("IDF", "IDO"):
                seen_idx.add(u)
            elif it in ("STF", "STO"):
                stock_names.add(u)
                day_has_stock = True
        for u in seen_idx:
            e = idx.setdefault(u, {"days": 0, "first": d, "last": d})
            e["days"] += 1
            e["last"] = d
        if day_has_stock:
            stock_days += 1

    unders = [{"underlying": u, "days": e["days"], "first": e["first"], "last": e["last"]}
              for u, e in sorted(idx.items(), key=lambda kv: -kv[1]["days"])]

    gaps = ["Intraday / 1-min data absent — validates held-to-theta, not scalpers"]
    if not stock_names:
        gaps.insert(0, "Stock (STO/STF) derivatives not ingested — index only")

    return {
        "n_days": len(days),
        "first": days[0] if days else None,
        "last": days[-1] if days else None,
        "underlyings": unders,
        "stock_universe": {
            "count": len(stock_names),
            "days_with_stock_data": stock_days,
            "sample": sorted(stock_names)[:25],
        },
        # Full set the OOS gate checks against — indices AND stocks, so a stock
        # strategy is graded instead of being rejected as "no data".
        "present_underlyings": sorted(set(idx.keys()) | stock_names),
        "gaps": gaps,
    }


def _iv_surfaces(store: BhavcopyStore, days: List[str], present: set[str]) -> Dict[str, Any]:
    samples = []
    latest = days[-1] if days else None
    for u in [x for x in BASE_RATE_UNDERLYINGS if x in present]:
        surf = richness_zscore(store, u, latest) if latest else {"available": False}
        summary = surf.get("summary") or {}
        richness = surf.get("richness") or {}
        samples.append({
            "underlying": u,
            "date": latest,
            "available": bool(surf.get("available")),
            "spot": surf.get("spot"),
            "atm_iv": surf.get("atm_iv"),
            "near_expiry": summary.get("near_expiry"),
            "put_call_skew": summary.get("put_call_skew"),
            "point_count": summary.get("point_count"),
            "richness": richness,
        })
    return {"date": latest, "samples": samples,
            "note": "ATM IV richness is computed from stored bhavcopy option chains; it is evidence for gates, not a trade by itself."}


# ---- per-strategy OOS verdict ------------------------------------------------

def _strat_underlying(s: Dict[str, Any]) -> str:
    vc = s.get("visual_config") or {}
    opt = vc.get("options") or {}
    return (opt.get("underlying") or vc.get("symbol") or "NIFTY").upper()


def _oos(strategies: List[Dict[str, Any]], store: BhavcopyStore,
         present: Optional[set] = None) -> Dict[str, Any]:
    rows = []
    # CACHE LOCALITY — grade all strategies on one underlying before moving to the
    # next. The store's per-(underlying, day) cache comfortably holds ONE
    # underlying's full history, so grouped order means each underlying is parsed
    # once and every later strategy on it runs warm (measured 127s cold → 14s warm).
    # In arbitrary order the cache evicts between underlyings and every strategy
    # re-parses — that is what made OOS run 1h20m+ without finishing.
    ordered = sorted(strategies, key=lambda s: _strat_underlying(s))
    total = len(ordered)
    t_stage = time.time()
    for i, s in enumerate(ordered, 1):
        # Skip strategies whose underlying has no data (equity/stocks) WITHOUT paying
        # a full ~18s backtest just to surface the same "no data" error.
        if present is not None and _strat_underlying(s) not in present:
            rows.append({"name": s.get("name"), "underlying": _strat_underlying(s),
                         "error": "underlying not present in the bhavcopy store"})
            print(f"[edge_lab]   oos {i}/{total} {_strat_underlying(s)} "
                  f"{str(s.get('name'))[:38]} — skipped (no data)", flush=True)
            continue
        t0 = time.time()
        judged = judge_grade(s, mode="eod", store=store)
        # Per-strategy progress: without this the stage is a black box for its whole
        # runtime and the only way to answer "how long?" is to guess.
        print(f"[edge_lab]   oos {i}/{total} {_strat_underlying(s)} "
              f"{str(s.get('name'))[:38]} — {time.time()-t0:.0f}s "
              f"(stage {time.time()-t_stage:.0f}s)", flush=True)
        if judged.get("status") == "error":
            rows.append({"name": s.get("name"), "error": judged.get("error")})
            continue
        o = judged.get("overall") or {}
        rows.append({
            "name": judged.get("name"), "underlying": judged.get("underlying"),
            "structure": judged.get("structure"), "verdict": judged.get("verdict"),
            "n": o["n"], "expectancy": o["expectancy"], "win_rate": o["win_rate"],
            "std": o.get("std"), "t_stat": o.get("t_stat"), "sharpe": o.get("sharpe"),
            "pnl": o["pnl"], "oos_year": judged.get("oos_year"),
            "oos_expectancy": (judged.get("oos") or {}).get("expectancy", 0),
            "pct_green_months": judged.get("pct_green_months"),
            "regime_breakdown": judged.get("regime_breakdown") or {},
            "signals": judged.get("signals", 0),
            "signal_evaluation": judged.get("signal_evaluation"),
            "trade_returns": [float(t.get("pnl") or 0.0) for t in (judged.get("trades") or [])],
        })
    order = {"CANDIDATE_EDGE": 0, "FRAGILE": 1, "INSUFFICIENT_DATA": 2, "NO_EDGE_NEGATIVE": 3}
    rows.sort(key=lambda r: order.get(r.get("verdict", ""), 9))
    counts: Dict[str, int] = {}
    for r in rows:
        v = r.get("verdict") or ("ERROR" if r.get("error") else "?")
        counts[v] = counts.get(v, 0) + 1
    return {"rows": rows, "counts": counts}


# ---- exit-geometry sweep -----------------------------------------------------

def _sweep(strategies: List[Dict[str, Any]], store: BhavcopyStore,
           present: Optional[set] = None) -> List[Dict[str, Any]]:
    engine = EODOptionsBacktest(store)
    if present is not None:
        strategies = [s for s in strategies if _strat_underlying(s) in present]
    targets = [s for s in strategies
               if "theta credit spread" in (s.get("name") or "").lower()
               and any(u in (s.get("name") or "").upper() for u in ("NIFTY", "BANKNIFTY"))]
    if not targets:  # fallback: any credit-spread strategies
        targets = [s for s in strategies
                   if (((s.get("visual_config") or {}).get("options") or {}).get("structure")
                       == "credit_spread")][:2]
    grid = list(itertools.product(_CREDIT_TP, _CREDIT_SL, _WIDTH, _MAX_DTE))
    out = []
    for ti, strat in enumerate(targets, 1):
        cells = []
        pos_oos = edges = 0
        t0 = time.time()
        print(f"[edge_lab]   sweep {ti}/{len(targets)} {str(strat.get('name'))[:38]} "
              f"— {len(grid)} configs", flush=True)
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
        print(f"[edge_lab]   sweep {ti}/{len(targets)} done in {time.time()-t0:.0f}s "
              f"({len(cells)} configs, {pos_oos} positive-OOS, {edges} candidate-edge)",
              flush=True)
        out.append({"name": strat.get("name"), "configs": len(cells),
                    "positive_oos": pos_oos, "candidate_edges": edges, "cells": cells})
    return out


# ---- top-level ---------------------------------------------------------------

def _store_fingerprint(store: BhavcopyStore, days: List[str]) -> str:
    """Identity of the store's data: day count + newest day + total bytes. The byte
    total catches a RE-INGEST that rewrites existing days with more contracts (e.g.
    backfilling the full stock universe) — day-count alone would miss it and serve a
    stale cache. ~1860 stat() calls, sub-second, vs the 11-min scan it guards."""
    if not days:
        return "empty"
    total = 0
    try:
        import glob
        for p in glob.glob(os.path.join(store.root, "*", "*.csv.gz")):
            total += os.path.getsize(p)
    except Exception:  # noqa: BLE001
        total = -1
    return f"{len(days)}:{days[-1]}:{total}"


def _store_derived_stages(store: BhavcopyStore, days: List[str]) -> Dict[str, Any]:
    """coverage + iv_surface + base_rate for the whole store, cached on disk by the
    store fingerprint. Reused across rebuilds until the store grows, turning the
    ~11-min scan into a one-time-per-ingest cost. Cache failures fall back to a live
    compute — never fatal."""
    fp = _store_fingerprint(store, days)
    cache_path = None
    try:
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(store.root)), "edge_lab_cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "store_stages.json")
        if os.path.exists(cache_path):
            with open(cache_path) as fh:
                cached = json.load(fh)
            if cached.get("fingerprint") == fp:
                cached["cache_hit"] = True
                return cached
    except Exception:  # noqa: BLE001
        pass
    coverage = _coverage(store, days)
    present = {u["underlying"] for u in coverage.get("underlyings", [])}
    iv_surface = _iv_surfaces(store, days, present)
    base_rate = {"short_vol": _base_rate(store), "directional": _directional(store), "slippage_pct": _SLIP}
    result = {"fingerprint": fp, "coverage": coverage, "iv_surface": iv_surface,
              "base_rate": base_rate, "cache_hit": False}
    if cache_path:
        try:
            with open(cache_path, "w") as fh:
                json.dump(result, fh)
        except Exception:  # noqa: BLE001
            pass
    return result


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

    active_strategies = [s for s in strategies if str(s.get("status") or "").lower() != "archived"]
    archived_count = len(strategies) - len(active_strategies)
    # coverage + iv_surface + base_rate scan the WHOLE store (~11 min) and depend
    # only on the data, not the strategies — cache them by a store fingerprint so a
    # rebuild after a strategy edit reuses them; only OOS/sweep recompute each time.
    store_stages = _phase("store_stages", lambda: _store_derived_stages(store, days))
    coverage = store_stages["coverage"]
    iv_surface = store_stages["iv_surface"]
    base_rate = store_stages["base_rate"]
    # Real store contents (indices + every backfilled stock), NOT the old hard-coded
    # index list — otherwise a stock strategy is falsely rejected as "no data".
    present = set(coverage.get("present_underlyings")
                  or [u["underlying"] for u in coverage.get("underlyings", [])])
    oos = _phase("oos", lambda: _oos(active_strategies, store, present))
    sweep = _phase("sweep", lambda: _sweep(active_strategies, store, present)) if include_sweep else None
    from core.edge_research_ledger import enrich_snapshot
    return enrich_snapshot({"status": "ready", "generated_at": now, "coverage": coverage,
                            "iv_surface": iv_surface, "base_rate": base_rate, "oos": oos, "sweep": sweep,
                            "book": {"active_strategies": len(active_strategies), "archived_strategies": archived_count}})
