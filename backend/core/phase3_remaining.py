"""ERP Phase 3 remaining sleeve validators (P3-4..P3-7).

Research-only utilities for PEAD, participant-OI overlays, paused-seller
re-judge, and multi-sleeve book assembly. No DB writes, broker calls, or
strategy wakeups.
"""
from __future__ import annotations

from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Optional

from core.bhavcopy_store import BhavcopyStore
from core.daily_delta1_momentum import grade_universe as grade_delta1
from core.edge_research_ledger import deflated_sharpe
from core.earnings_calendar import events_for
from core.earnings_iv_crush import grade_universe as grade_earnings_iv
from core.eod_options_backtest import EODOptionsBacktest, walk_forward
from core.india_flows import get_participant_oi, participant_oi_days
from core.slow_premium_core import grade_slow_premium


def _returns(candles: List[Dict[str, Any]], start_idx: int, days: int) -> Optional[float]:
    end_idx = start_idx + days
    if start_idx < 0 or end_idx >= len(candles):
        return None
    start = float(candles[start_idx].get("close") or 0)
    end = float(candles[end_idx].get("close") or 0)
    if not start:
        return None
    return (end - start) / start


def grade_pead(
    symbols: Iterable[str],
    *,
    store: Optional[BhavcopyStore] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    hold_days: int = 5,
) -> Dict[str, Any]:
    """P3-4: PEAD hypothesis card #1.

    We do not have surprise magnitude yet, so this is the honest preliminary
    test: after an earnings event, follow the stock future's event-day direction
    for `hold_days`. Flat/negative result kills the PEAD idea until surprise data
    exists.
    """
    store = store or BhavcopyStore()
    rows = []
    all_pnls: List[float] = []
    for sym in symbols:
        candles = store.underlying_daily(sym.upper(), start, end)
        by_day = {c["date"][:10]: i for i, c in enumerate(candles)}
        pnls = []
        events = events_for(sym, start or "1900-01-01", end or "2999-12-31")
        for ev in events:
            day = str(ev.get("date") or "")[:10]
            i = by_day.get(day)
            if i is None or i <= 0:
                continue
            event_ret = _returns(candles, i - 1, 1)
            fwd = _returns(candles, i, hold_days)
            if event_ret is None or fwd is None or abs(event_ret) < 0.002:
                continue
            signed = fwd if event_ret > 0 else -fwd
            pnl = signed * 100_000.0
            pnls.append(pnl)
            all_pnls.append(pnl)
        rows.append({"symbol": sym.upper(), "events": len(events), "n": len(pnls),
                     "expectancy": round(mean(pnls), 2) if pnls else 0.0,
                     "win_rate": round(100 * sum(1 for p in pnls if p > 0) / len(pnls), 1) if pnls else 0.0})
    n = len(all_pnls)
    exp = mean(all_pnls) if all_pnls else 0.0
    dsr = deflated_sharpe({"n": n, "expectancy": exp, "oos_expectancy": exp, "pct_green_months": 50}, trials_tested=1)
    verdict = "CANDIDATE_EDGE" if n >= 100 and exp > 0 and dsr["passed"] else "NO_EDGE_NEGATIVE" if n >= 30 and exp <= 0 else "INSUFFICIENT_DATA"
    return {"task": "P3-4", "name": "S1b PEAD validation", "status": "ready",
            "overall": {"n": n, "expectancy": round(exp, 2),
                        "win_rate": round(100 * sum(1 for p in all_pnls if p > 0) / n, 1) if n else 0.0},
            "deflated_sharpe": dsr, "verdict": verdict,
            "paper_gate": {"eligible_for_paper": bool(verdict == "CANDIDATE_EDGE" and dsr["passed"])},
            "rows": rows}


def _participant_bias(row: Dict[str, Any]) -> float:
    rows = row.get("rows") or []
    by = {str(r.get("participant") or "").upper(): r for r in rows}
    fii = by.get("FII") or {}
    client = by.get("CLIENT") or {}
    def val(rec, key):
        try:
            return float(rec.get(key) or 0)
        except Exception:  # noqa: BLE001
            return 0.0
    return (val(fii, "future_index_long") - val(fii, "future_index_short")
            - 0.25 * (val(client, "future_index_long") - val(client, "future_index_short")))


def grade_participant_oi_overlay(*, store: Optional[BhavcopyStore] = None,
                                 underlying: str = "NIFTY", start: Optional[str] = None,
                                 end: Optional[str] = None, horizon_days: int = 5) -> Dict[str, Any]:
    """P3-5: validate participant-OI as a directional overlay, not a premise."""
    store = store or BhavcopyStore()
    candles = store.underlying_daily(underlying.upper(), start, end)
    by_day = {c["date"][:10]: i for i, c in enumerate(candles)}
    pnls = []
    used = []
    for day in participant_oi_days():
        if (start and day < start) or (end and day > end) or day not in by_day:
            continue
        row = get_participant_oi(day)
        if not row:
            continue
        bias = _participant_bias(row)
        if abs(bias) <= 0:
            continue
        fwd = _returns(candles, by_day[day], horizon_days)
        if fwd is None:
            continue
        pnl = (fwd if bias > 0 else -fwd) * 100_000.0
        pnls.append(pnl)
        used.append({"date": day, "bias": round(bias, 2), "pnl": round(pnl, 2)})
    n = len(pnls)
    exp = mean(pnls) if pnls else 0.0
    dsr = deflated_sharpe({"n": n, "expectancy": exp, "oos_expectancy": exp, "pct_green_months": 50}, trials_tested=1)
    verdict = "CANDIDATE_EDGE" if n >= 100 and exp > 0 and dsr["passed"] else "NO_EDGE_NEGATIVE" if n >= 30 and exp <= 0 else "INSUFFICIENT_DATA"
    return {"task": "P3-5", "name": "S5 participant-OI overlay", "status": "ready",
            "underlying": underlying.upper(), "overall": {"n": n, "expectancy": round(exp, 2),
            "win_rate": round(100 * sum(1 for p in pnls if p > 0) / n, 1) if n else 0.0},
            "deflated_sharpe": dsr, "verdict": verdict,
            "overlay_gate": {"wire_overlay": bool(verdict == "CANDIDATE_EDGE" and dsr["passed"])},
            "sample": used[:10]}


def _seller_config(name: str, underlying: str, short_otm_pct: float = 0.03,
                   wing_width: int = 6) -> Dict[str, Any]:
    return {"id": name.lower().replace(" ", "-"), "name": name,
            "python_code": "def run(data):\n    return [{'date': c['date'], 'action': 'BUY'} for c in data]\n",
            "visual_config": {"symbol": underlying, "options": {"underlying": underlying,
            "structure": "credit_spread", "short_otm_pct": short_otm_pct,
            "wing_width": wing_width, "spread_width": wing_width, "exit_mode": ""}}}


def rejudge_paused_sellers(*, store: Optional[BhavcopyStore] = None,
                           start: Optional[str] = None, end: Optional[str] = None) -> Dict[str, Any]:
    """P3-6: re-derived 0.50/0.90 geometry plus slippage stress.

    This is a broad EOD proxy for paused sellers; the intraday QG-O11 proof still
    belongs to the minute OOS runner. Founder wake/delete remains external.
    """
    store = store or BhavcopyStore()
    specs = [
        ("QG-O11 NIFTY Regime Seller proxy", "NIFTY", 0.01, 1),
        ("RAE NIFTY Range Seller proxy", "NIFTY", 0.03, 6),
        ("RAE BANKNIFTY Range Seller proxy", "BANKNIFTY", 0.03, 6),
        ("RAE SENSEX Range Seller proxy", "SENSEX", 0.03, 6),
        ("QG-O4 SENSEX artifact proxy", "SENSEX", 0.02, 4),
    ]
    rows = []
    for name, u, otm, wing in specs:
        cfg = _seller_config(name, u, otm, wing)
        base = None
        stresses = []
        for slip in (0.02, 0.05, 0.08):
            res = EODOptionsBacktest(store).run(cfg, start=start, end=end, params={
                "short_otm_pct": otm, "wing_width": wing, "width": wing,
                "credit_tp": 0.50, "credit_sl": 0.90, "min_dte": 2, "max_dte": 8,
                "max_hold_days": 8, "slippage_pct": slip,
            })
            if res.get("error"):
                stresses.append({"slippage": slip, "error": res["error"]})
                continue
            wf = walk_forward(res)
            o = wf.get("overall") or {}
            item = {"slippage": slip, "n": o.get("n", 0), "expectancy": o.get("expectancy", 0),
                    "verdict": wf.get("verdict")}
            stresses.append(item)
            base = base or item
        pass_all = bool(stresses and all(s.get("expectancy", -1) > 0 and s.get("n", 0) >= 30 for s in stresses if not s.get("error")))
        rows.append({"name": name, "underlying": u, "base": base, "slippage_stress": stresses,
                     "founder_action": "eligible_to_wake" if pass_all else "keep_paused_or_delete_if_repeated_fail"})
    return {"task": "P3-6", "name": "Paused-seller re-judge", "status": "ready", "rows": rows}


def assemble_book(*, store: Optional[BhavcopyStore] = None, start: Optional[str] = None,
                  end: Optional[str] = None) -> Dict[str, Any]:
    """P3-7: combine sleeve verdicts into a non-mutating book assembly."""
    store = store or BhavcopyStore()
    sleeve_results = [
        grade_earnings_iv(["RELIANCE"], store=store, start=start, end=end),
        grade_delta1(["NIFTY"], store=store, start=start, end=end),
        grade_slow_premium(store=store, start=start, end=end, entry_stride_days=60),
        grade_participant_oi_overlay(store=store, start=start, end=end),
    ]
    candidates = []
    for block in sleeve_results:
        for row in block.get("rows", []):
            gate = row.get("paper_gate") or row.get("overlay_gate") or {}
            overall = row.get("overall") or {}
            exp = float(overall.get("expectancy") or 0.0)
            if gate.get("eligible_for_paper") or gate.get("wire_overlay"):
                candidates.append({"name": row.get("name") or row.get("symbol") or row.get("underlying"),
                                   "expectancy": exp, "n": overall.get("n", 0)})
    heat = sum(max(0.0, c["expectancy"]) for c in candidates)
    return {"task": "P3-7", "name": "Phase 3 book assembly", "status": "ready",
            "candidates": candidates, "portfolio": {"candidate_count": len(candidates),
            "heat_score": round(heat, 2), "paper_book_ready": bool(candidates),
            "correlation_cap": "max one active sleeve per underlying until live correlation evidence exists"},
            "sleeve_results": sleeve_results}
