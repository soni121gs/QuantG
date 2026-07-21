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
from core import regime_taxonomy as tax

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
# Each mode is a DIFFERENT edge source with its OWN regime + IV gate — this is
# the RAE ensemble (§18): one specialist per day-type, standing down elsewhere.
#   put_sell/call_sell/condor_sell : VRP harvest       — RANGE + IV rich (z≥min_z)
#   trend_delta1                   : trend continuation — TREND + IV cheap (z≤-min_z)
#   reversal_long                  : mean-reversion     — RANGE + over-extended, fade the spike
#   calendar_neutral               : IV TERM-STRUCTURE  — RANGE + low-vol, sell near/buy far
#   longvol                        : LONG GAMMA         — HIGH_VOL + IV cheap, ride the expansion
# The gate IS the strategy — see §18.4 (precision > payoff). NONE of these is
# "sell premium renamed": reversal is contrarian, calendar harvests a different
# (time) axis, longvol is long-vol (the mirror of VRP).

_LARGE_MOVE = 0.012   # |daily return| that counts as an over-extension / expansion


def build_gated_signals(candles: List[Dict[str, Any]], *, store: BhavcopyStore,
                        underlying: str, mode: str, min_z: float = 0.75,
                        require_low_vol: bool = True) -> Dict[str, Any]:
    regimes = {r["date"]: r for r in tag_regimes(candles)}
    signals: List[Dict[str, Any]] = []
    rejected = {"wrong_regime": 0, "iv_not_rich": 0, "iv_not_cheap": 0, "iv_not_neutral": 0,
                "surface_unavailable": 0, "high_vol": 0, "not_extended": 0, "not_expanding": 0}
    prev_close: Optional[float] = None

    for c in candles:
        day = str(c.get("date"))[:10]
        reg = regimes.get(day) or {}
        trend = reg.get("trend")
        vol = reg.get("vol")
        close = float(c.get("close") or 0.0)
        ret = (close / prev_close - 1.0) if prev_close else 0.0
        prev_close = close or prev_close
        rich = _richness(store, underlying, day)
        if not rich.get("available"):
            rejected["surface_unavailable"] += 1
            continue
        z = float(rich.get("zscore") or 0.0)

        # --- trend continuation (deep-ITM delta-1) : TREND + IV cheap ---------
        if mode == "trend_delta1":
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

        # --- mean-reversion (contrarian debit spread) : RANGE + over-extended -
        if mode == "reversal_long":
            if trend != "RANGE":
                rejected["wrong_regime"] += 1
                continue
            if abs(ret) < _LARGE_MOVE:
                rejected["not_extended"] += 1
                continue
            # fade the spike: sharp DOWN -> buy CE bounce; sharp UP -> buy PE fade
            direction = "CE" if ret < 0 else "PE"
            signals.append({"date": c["date"], "action": "BUY" if direction == "CE" else "SELL",
                            "direction": direction, "ret_pct": round(ret * 100, 2),
                            "iv_z": round(z, 3), "gate": "range+overextended_fade"})
            continue

        # --- IV term-structure (calendar) : RANGE + low-vol, premium not-cheap -
        if mode == "calendar_neutral":
            if trend != "RANGE":
                rejected["wrong_regime"] += 1
                continue
            if vol == "HIGH_VOL":
                rejected["high_vol"] += 1
                continue
            if z < -min_z:   # skip only when premium is outright cheap (bad for a net-debit calendar)
                rejected["iv_not_neutral"] += 1
                continue
            signals.append({"date": c["date"], "action": "BUY", "direction": "CE",
                            "iv_z": round(z, 3), "gate": "range+lowvol_calendar"})
            continue

        # --- long gamma (debit spread) : HIGH_VOL + IV cheap, ride expansion ---
        if mode == "longvol":
            if vol != "HIGH_VOL":
                rejected["wrong_regime"] += 1
                continue
            if z > -min_z:
                rejected["iv_not_cheap"] += 1
                continue
            if abs(ret) < _LARGE_MOVE:
                rejected["not_expanding"] += 1
                continue
            direction = "CE" if ret > 0 else "PE"   # ride the direction of the expansion
            signals.append({"date": c["date"], "action": "BUY" if direction == "CE" else "SELL",
                            "direction": direction, "ret_pct": round(ret * 100, 2),
                            "iv_z": round(z, 3), "gate": "highvol+iv_cheap_longgamma"})
            continue

        # --- VRP sellers : RANGE + rich premium (+ optionally avoid high-vol) --
        if trend != "RANGE":
            rejected["wrong_regime"] += 1
            continue
        if require_low_vol and vol == "HIGH_VOL":
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

    # ── DIFFERENT LOGICS (not premium selling) — one per remaining regime ──────
    # 8. NIFTY mean-reversion: fade an over-extended day back into the range (contrarian)
    {"cfg": _cfg("IDX-NIFTY-REVERSAL", "IDX NIFTY Mean-Reversion Fade (RANGE overext)", "NIFTY",
                 {"structure": "debit_spread", "spread_width": 4, "exit_mode": ""},
                 {"target_pct": 50.0, "stoploss_pct": 50.0, "max_hold_days": 4}),
     "mode": "reversal_long",
     "params": {"width": 4, "min_dte": 2, "max_dte": 8, "max_hold_days": 4,
                "debit_tp": 0.5, "debit_sl": 0.5}},

    # 9. SENSEX mean-reversion fade
    {"cfg": _cfg("IDX-SENSEX-REVERSAL", "IDX SENSEX Mean-Reversion Fade (RANGE overext)", "SENSEX",
                 {"structure": "debit_spread", "spread_width": 4, "exit_mode": ""},
                 {"target_pct": 50.0, "stoploss_pct": 50.0, "max_hold_days": 4}),
     "mode": "reversal_long",
     "params": {"width": 4, "min_dte": 2, "max_dte": 8, "max_hold_days": 4,
                "debit_tp": 0.5, "debit_sl": 0.5}},

    # 10. NIFTY IV term-structure calendar: sell rich near-expiry vs cheaper far (time axis)
    {"cfg": _cfg("IDX-NIFTY-CALENDAR", "IDX NIFTY IV Term-Structure Calendar (RANGE lowvol)", "NIFTY",
                 {"structure": "calendar_spread", "exit_mode": ""},
                 {"max_hold_days": 6}),
     "mode": "calendar_neutral",
     "params": {"min_dte": 2, "max_dte": 8, "calendar_far_min_dte": 21, "calendar_far_max_dte": 60,
                "max_hold_days": 6, "debit_tp": 0.5, "debit_sl": 0.5}},

    # 11. SENSEX IV term-structure calendar
    {"cfg": _cfg("IDX-SENSEX-CALENDAR", "IDX SENSEX IV Term-Structure Calendar (RANGE lowvol)", "SENSEX",
                 {"structure": "calendar_spread", "exit_mode": ""},
                 {"max_hold_days": 10}),
     "mode": "calendar_neutral",
     "params": {"min_dte": 3, "max_dte": 12, "calendar_far_min_dte": 25, "calendar_far_max_dte": 60,
                "max_hold_days": 10, "debit_tp": 0.5, "debit_sl": 0.5}},

    # 12. NIFTY long-gamma coverage: ride the expansion on a cheap-IV high-vol day
    {"cfg": _cfg("IDX-NIFTY-LONGVOL", "IDX NIFTY Long-Gamma (HIGH_VOL + IV cheap)", "NIFTY",
                 {"structure": "debit_spread", "spread_width": 5, "exit_mode": ""},
                 {"target_pct": 70.0, "stoploss_pct": 40.0, "max_hold_days": 3}),
     "mode": "longvol",
     "params": {"width": 5, "min_dte": 2, "max_dte": 8, "max_hold_days": 3,
                "debit_tp": 0.7, "debit_sl": 0.4}},

    # 13. SENSEX long-gamma coverage
    {"cfg": _cfg("IDX-SENSEX-LONGVOL", "IDX SENSEX Long-Gamma (HIGH_VOL + IV cheap)", "SENSEX",
                 {"structure": "debit_spread", "spread_width": 5, "exit_mode": ""},
                 {"target_pct": 70.0, "stoploss_pct": 40.0, "max_hold_days": 3}),
     "mode": "longvol",
     "params": {"width": 5, "min_dte": 2, "max_dte": 8, "max_hold_days": 3,
                "debit_tp": 0.7, "debit_sl": 0.4}},
]

# Regime coverage map — what each sleeve is FOR (the ensemble, not one bet):
#   RANGE (60% of days)   : put_sell, call_sell, condor_sell, calendar_neutral, reversal_long
#   TREND (~1%)           : trend_delta1
#   HIGH_VOL_CHOP (13%)   : longvol   (else stand down — see §18.4)
#   INSIDE_QUIET (26%)    : reversal_long + sellers (RANGE-adjacent)
#
# 2026-07-21: the map above used to live ONLY in this comment. The RAE router reads
# `visual_config.options.specialist_role`, and signal_manager defaults an UNTAGGED
# spread to 'range_seller' — so every sleeve seeded from here was silently routed as
# a premium seller regardless of what it actually is. That inverted the long-gamma
# sleeve (a volatility-EXPANSION structure permitted only on calm range days, and
# stood down on trend: 0 trades ever). Encode the map as DATA so the router can read
# it. `owned_regimes` is carried alongside so the Diagnostician's
# static.specialist_tag_consistency probe covers these rows.
_ROLE_BY_MODE: Dict[str, Dict[str, Any]] = {
    "put_sell":         {"role": tax.REGIME_OWNER[tax.RANGE],
                         "owned": [tax.RANGE, tax.INSIDE_QUIET]},
    "call_sell":        {"role": tax.REGIME_OWNER[tax.RANGE],
                         "owned": [tax.RANGE, tax.INSIDE_QUIET]},
    "condor_sell":      {"role": tax.REGIME_OWNER[tax.RANGE],
                         "owned": [tax.RANGE, tax.INSIDE_QUIET]},
    "calendar_neutral": {"role": tax.REGIME_OWNER[tax.RANGE],
                         "owned": [tax.RANGE, tax.INSIDE_QUIET]},
    "reversal_long":    {"role": tax.REGIME_OWNER[tax.INSIDE_QUIET],
                         "owned": [tax.INSIDE_QUIET, tax.RANGE]},
    "trend_delta1":     {"role": "trend_delta1",
                         "owned": [tax.TREND_UP, tax.TREND_DOWN]},
    "longvol":          {"role": tax.LONG_VOL_ROLE,
                         "owned": [tax.HIGH_VOL_CHOP]},
}

for _sleeve in SLEEVES:
    _tags = _ROLE_BY_MODE.get(_sleeve["mode"])
    if _tags:
        _opts = _sleeve["cfg"]["visual_config"]["options"]
        _opts.setdefault("specialist_role", _tags["role"])
        _opts.setdefault("owned_regimes", list(_tags["owned"]))
REGIME_COVERAGE = {
    "RANGE": ["put_sell", "call_sell", "condor_sell", "calendar_neutral", "reversal_long"],
    "TREND": ["trend_delta1"],
    "HIGH_VOL": ["longvol"],
    "INSIDE": ["reversal_long", "put_sell"],
}


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


# ── monotonic-gate sweep ─────────────────────────────────────────────────────
# The QG-O1 tell (§15.5): a REAL gated edge has expectancy that RISES MONOTONICALLY
# as the gate tightens (stricter z → fatter per-trade edge, thinner sample). A
# flat/noisy expectancy-vs-gate curve is a fluke, not a signal. This runs each
# sleeve across a z-grid and reports the curve so the shape is visible.

DEFAULT_Z_GRID = [0.5, 0.75, 1.0, 1.25, 1.5]


def _monotonic(values: List[Optional[float]]) -> Dict[str, Any]:
    seq = [v for v in values if v is not None]
    if len(seq) < 3:
        return {"monotonic_up": False, "reason": "insufficient points"}
    rising = sum(1 for a, b in zip(seq, seq[1:]) if b >= a)
    return {"monotonic_up": bool(rising >= len(seq) - 2 and seq[-1] > seq[0]),
            "rising_steps": rising, "steps": len(seq) - 1,
            "first": seq[0], "last": seq[-1]}


def sweep_min_z(*, store: Optional[BhavcopyStore] = None, start: Optional[str] = None,
                end: Optional[str] = None, z_grid: Optional[List[float]] = None) -> Dict[str, Any]:
    store = store or BhavcopyStore()
    grid = z_grid or DEFAULT_Z_GRID
    curves: Dict[str, Dict[str, Any]] = {}
    for entry in SLEEVES:
        name = entry["cfg"]["name"]
        exps: List[Optional[float]] = []
        ns: List[int] = []
        for z in grid:
            row = _grade_sleeve(entry, store=store, start=start, end=end, min_z=z)
            if row.get("status") == "error":
                exps.append(None)
                ns.append(0)
            else:
                overall = row.get("overall") or {}
                exps.append(overall.get("expectancy"))
                ns.append(int(overall.get("n") or 0))
        curves[name] = {"grid": grid, "expectancy": exps, "n": ns,
                        "monotonic": _monotonic(exps)}
    return {"z_grid": grid, "curves": curves,
            "confirmed_monotonic": [n for n, c in curves.items() if c["monotonic"].get("monotonic_up")]}
