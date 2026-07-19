"""ERP index-option alpha sleeves — NIFTY + SENSEX (research-only validators).

The census (CLAUDE.md §20) killed the old index book: one bet (short intraday
index premium) expressed ~11 ways in India's most algo-contested arena, at
credits below the cost floor. This module does NOT re-clone that bet. It encodes
the only two index edges the OOS work actually supports, and grades them the
honest way (regime/IV-gated, walk-forward, deflated-Sharpe, cost-floor):

  1. SELECTIVE VRP HARVEST (the QG-O1 winner, §15.5) — sell DEFINED-RISK premium
     ONLY when implied vol is richly above realized AND the regime is RANGE. The
     first-ever OOS pass came from GATE PRECISION, not from the structure. We
     spread that same gate across both tails (put/call spread), both indices
     (NIFTY weekly + SENSEX monthly), and the two-sided condor — independent
     bets (§20 breadth law), never more knobs on one bet.

  2. TREND-COVERAGE DELTA-1 (§18 regime-ensemble finding) — a deep-ITM single
     leg (≈delta-1, low theta — the fix for why every OTM buyer died 5×) that
     fires ONLY on IV-CHEAP + trend-aligned days. Not premium selling: the
     diversifying leg so the book stops bleeding on the ~1% of days that trend.

Every sleeve is DEFINED-RISK and passes the paper gate only if it is
CANDIDATE_EDGE out-of-sample, survives the deflated-Sharpe overfit penalty, AND
clears the cost-floor law (expected gross edge ≥ 3× modeled round-trip friction,
§20 law 2 / Carver). Nothing here wakes a registry row or touches the broker path
— it is a judge, run on the populated VPS bhavcopy store.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.bhavcopy_store import BhavcopyStore
from core.edge_research_ledger import deflated_sharpe
from core.eod_options_backtest import EODOptionsBacktest, walk_forward
from core.historical_regimes import tag_regimes

# §20 cost-floor law: an index option structure needs meaningful expected edge or
# friction eats it. Require gross expectancy ≥ COST_FLOOR_MULT × per-trade friction.
COST_FLOOR_MULT = 3.0


def _richness(store: BhavcopyStore, underlying: str, day: str) -> Dict[str, Any]:
    try:
        from core.iv_surface import richness_zscore
        return (richness_zscore(store, underlying, day) or {}).get("richness") or {"available": False}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": str(exc)}


# ── gated signal builder ────────────────────────────────────────────────────
# mode ∈ {put_sell, call_sell, condor_sell, trend_delta1}. Sellers require
# regime=RANGE + IV rich (z ≥ min_z). Trend-coverage requires an aligned trend +
# IV cheap (z ≤ -min_z). The gate IS the strategy — see §18.4 (precision > payoff).

def build_gated_signals(candles: List[Dict[str, Any]], *, store: BhavcopyStore,
                        underlying: str, mode: str, min_z: float = 0.75,
                        require_low_vol: bool = True) -> Dict[str, Any]:
    regimes = {r["date"]: r for r in tag_regimes(candles)}
    signals: List[Dict[str, Any]] = []
    rejected = {"wrong_regime": 0, "iv_not_rich": 0, "iv_not_cheap": 0,
                "surface_unavailable": 0, "high_vol": 0}

    for c in candles:
        day = str(c.get("date"))[:10]
        reg = regimes.get(day) or {}
        trend = reg.get("trend")
        rich = _richness(store, underlying, day)
        if not rich.get("available"):
            rejected["surface_unavailable"] += 1
            continue
        z = float(rich.get("zscore") or 0.0)

        if mode == "trend_delta1":
            # coverage leg: only on a real trend, and only when premium is CHEAP
            if trend not in ("UPTREND", "DOWNTREND"):
                rejected["wrong_regime"] += 1
                continue
            if z > -min_z:
                rejected["iv_not_cheap"] += 1
                continue
            direction = "CE" if trend == "UPTREND" else "PE"
            signals.append({"date": c["date"], "action": "BUY" if direction == "CE" else "SELL",
                            "direction": direction, "iv_z": round(z, 3), "trend": trend,
                            "gate": "trend_aligned+iv_cheap"})
            continue

        # sellers: RANGE + rich premium (+ optionally avoid high-vol tape)
        if trend != "RANGE":
            rejected["wrong_regime"] += 1
            continue
        if require_low_vol and reg.get("vol") == "HIGH_VOL":
            rejected["high_vol"] += 1
            continue
        if z < min_z:
            rejected["iv_not_rich"] += 1
            continue
        if mode == "put_sell":
            sig = {"action": "BUY", "direction": "PE"}      # sell PUT spread (bullish credit)
        elif mode == "call_sell":
            sig = {"action": "SELL", "direction": "CE"}     # sell CALL spread (bearish credit)
        else:  # condor_sell — direction-agnostic, engine ignores side for iron_condor
            sig = {"action": "SELL", "direction": "SHORT_VOL"}
        signals.append({"date": c["date"], **sig, "iv_z": round(z, 3), "trend": trend,
                        "gate": "range+iv_rich"})

    return {"signals": signals, "audit": {"underlying": underlying.upper(), "mode": mode,
                                          "min_z": min_z, "accepted": len(signals),
                                          "rejected": rejected}}


# ── sleeve configs (all defined-risk) ───────────────────────────────────────

def _cfg(sid: str, name: str, underlying: str, options: Dict[str, Any],
         risk: Dict[str, Any]) -> Dict[str, Any]:
    base_opts = {"underlying": underlying.upper(), "lots": 1}
    base_opts.update(options)
    return {"id": sid, "name": name, "python_code": "def run(data):\n    return []\n",
            "visual_config": {"symbol": underlying.upper(), "options": base_opts, "risk": risk}}


SLEEVES: List[Dict[str, Any]] = [
    # 1. NIFTY put-spread VRP (the proven QG-O1 core, now RANGE-gated explicitly)
    {"cfg": _cfg("IDX-NIFTY-PUTSPREAD", "IDX NIFTY VRP Put-Spread (RANGE+rich)", "NIFTY",
                 {"structure": "credit_spread", "short_otm_pct": 0.03, "spread_width": 10,
                  "wing_width": 10, "exit_mode": "expiry"},
                 {"exit_mode": "hold_to_expiry", "max_hold_days": 8}),
     "mode": "put_sell",
     "params": {"short_otm_pct": 0.03, "width": 10, "min_dte": 2, "max_dte": 8,
                "max_hold_days": 8, "exit_mode": "expiry"}},

    # 2. NIFTY call-spread VRP (upside skew, independent tail)
    {"cfg": _cfg("IDX-NIFTY-CALLSPREAD", "IDX NIFTY VRP Call-Spread (RANGE+rich)", "NIFTY",
                 {"structure": "credit_spread", "short_otm_pct": 0.03, "spread_width": 10,
                  "wing_width": 10, "exit_mode": "expiry"},
                 {"exit_mode": "hold_to_expiry", "max_hold_days": 8}),
     "mode": "call_sell",
     "params": {"short_otm_pct": 0.03, "width": 10, "min_dte": 2, "max_dte": 8,
                "max_hold_days": 8, "exit_mode": "expiry"}},

    # 3. NIFTY iron condor (two-sided defined-risk short vol)
    {"cfg": _cfg("IDX-NIFTY-CONDOR", "IDX NIFTY VRP Iron Condor (RANGE+rich)", "NIFTY",
                 {"structure": "iron_condor", "short_otm_pct": 0.025, "wing_width": 6,
                  "spread_width": 6, "exit_mode": "expiry"},
                 {"exit_mode": "hold_to_expiry", "max_hold_days": 8}),
     "mode": "condor_sell",
     "params": {"short_otm_pct": 0.025, "wing_width": 6, "width": 6, "min_dte": 2,
                "max_dte": 8, "max_hold_days": 8, "exit_mode": "expiry"}},

    # 4. SENSEX monthly iron condor (BSE Thursday cycle — cross-index breadth)
    {"cfg": _cfg("IDX-SENSEX-CONDOR", "IDX SENSEX Monthly Iron Condor (RANGE+rich)", "SENSEX",
                 {"structure": "iron_condor", "short_otm_pct": 0.025, "wing_width": 5,
                  "spread_width": 5, "exit_mode": "expiry"},
                 {"exit_mode": "hold_to_expiry", "max_hold_days": 35}),
     "mode": "condor_sell",
     "params": {"short_otm_pct": 0.025, "wing_width": 5, "width": 5, "min_dte": 15,
                "max_dte": 45, "max_hold_days": 35, "exit_mode": "expiry"}},

    # 5. SENSEX put-spread VRP (cross-index diversification of #1)
    {"cfg": _cfg("IDX-SENSEX-PUTSPREAD", "IDX SENSEX VRP Put-Spread (RANGE+rich)", "SENSEX",
                 {"structure": "credit_spread", "short_otm_pct": 0.03, "spread_width": 8,
                  "wing_width": 8, "exit_mode": "expiry"},
                 {"exit_mode": "hold_to_expiry", "max_hold_days": 35}),
     "mode": "put_sell",
     "params": {"short_otm_pct": 0.03, "width": 8, "min_dte": 15, "max_dte": 45,
                "max_hold_days": 35, "exit_mode": "expiry"}},

    # 6. NIFTY trend-coverage delta-1 (IV-cheap + trend-aligned)
    {"cfg": _cfg("IDX-NIFTY-TREND-D1", "IDX NIFTY Trend Delta-1 Coverage (TREND+cheap)", "NIFTY",
                 {"structure": "single_leg", "strike_mode": "ITM_BUY", "itm_offset_pct": 0.02,
                  "exit_mode": ""},
                 {"target_pct": 60.0, "stoploss_pct": 25.0, "max_hold_days": 5}),
     "mode": "trend_delta1",
     "params": {"itm_offset_pct": 0.02, "min_dte": 14, "max_dte": 45, "max_hold_days": 5,
                "debit_tp": 0.6, "debit_sl": 0.25}},

    # 7. SENSEX trend-coverage delta-1
    {"cfg": _cfg("IDX-SENSEX-TREND-D1", "IDX SENSEX Trend Delta-1 Coverage (TREND+cheap)", "SENSEX",
                 {"structure": "single_leg", "strike_mode": "ITM_BUY", "itm_offset_pct": 0.02,
                  "exit_mode": ""},
                 {"target_pct": 60.0, "stoploss_pct": 25.0, "max_hold_days": 5}),
     "mode": "trend_delta1",
     "params": {"itm_offset_pct": 0.02, "min_dte": 14, "max_dte": 45, "max_hold_days": 5,
                "debit_tp": 0.6, "debit_sl": 0.25}},
]


def _cost_floor(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """§20 law 2: modeled per-trade friction (gross−net) vs gross expectancy."""
    if not trades:
        return {"passed": False, "reason": "no trades"}
    frictions = [abs(float(t.get("gross_pnl", 0)) - float(t.get("pnl", 0))) for t in trades]
    grosses = [float(t.get("gross_pnl", 0)) for t in trades]
    friction = sum(frictions) / len(frictions)
    gross_exp = sum(grosses) / len(grosses)
    return {"passed": bool(gross_exp >= COST_FLOOR_MULT * friction and gross_exp > 0),
            "gross_expectancy": round(gross_exp, 1), "friction_per_trade": round(friction, 1),
            "mult": round(gross_exp / friction, 2) if friction else None,
            "required_mult": COST_FLOOR_MULT}


def _grade_sleeve(entry: Dict[str, Any], *, store: BhavcopyStore, start: Optional[str],
                  end: Optional[str], min_z: float) -> Dict[str, Any]:
    cfg = entry["cfg"]
    underlying = (cfg["visual_config"]["options"]["underlying"])
    candles = store.underlying_daily(underlying, start, end)
    if len(candles) < 80:
        return {"name": cfg["name"], "underlying": underlying, "status": "error",
                "error": f"insufficient bhavcopy history for {underlying} ({len(candles)} days)"}
    sig = build_gated_signals(candles, store=store, underlying=underlying,
                              mode=entry["mode"], min_z=min_z)
    res = EODOptionsBacktest(store).run(cfg, start=start, end=end, params=entry["params"],
                                        signals=sig["signals"])
    if res.get("error"):
        return {"name": cfg["name"], "underlying": underlying, "status": "error",
                "error": res["error"], "selector": sig["audit"]}
    wf = walk_forward(res)
    overall = wf.get("overall") or {}
    dsr = deflated_sharpe({"n": overall.get("n"), "expectancy": overall.get("expectancy"),
                           "oos_expectancy": (wf.get("oos") or {}).get("expectancy"),
                           "pct_green_months": wf.get("pct_green_months")})
    cost_floor = _cost_floor(res.get("trades") or [])
    eligible = bool(wf.get("verdict") == "CANDIDATE_EDGE" and dsr.get("passed")
                    and cost_floor.get("passed"))
    return {
        "name": cfg["name"], "underlying": underlying, "status": "ready",
        "mode": entry["mode"], "structure": res.get("structure"),
        "selector": sig["audit"], "verdict": wf.get("verdict"), "overall": overall,
        "oos": wf.get("oos"), "oos_year": wf.get("oos_year"),
        "pct_green_months": wf.get("pct_green_months"),
        "regime_breakdown": res.get("regime_breakdown"),
        "paper_gate": {"eligible_for_paper": eligible, "deflated_sharpe": dsr,
                       "cost_floor": cost_floor, "iv_gate": {"min_z": min_z}},
    }


def grade_index_alpha(*, store: Optional[BhavcopyStore] = None, start: Optional[str] = None,
                      end: Optional[str] = None, min_z: float = 0.75) -> Dict[str, Any]:
    store = store or BhavcopyStore()
    rows = [_grade_sleeve(e, store=store, start=start, end=end, min_z=min_z) for e in SLEEVES]
    return {"count": len(rows),
            "eligible_for_paper": sum(1 for r in rows
                                      if (r.get("paper_gate") or {}).get("eligible_for_paper")),
            "rows": rows}
