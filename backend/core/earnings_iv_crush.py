"""ERP P3-1 earnings IV-crush research sleeve.

Builds deterministic T-1 earnings-event signals for defined-risk short-vol
structures and grades them through the existing event/EOD judge. This module is
research-only: it never writes strategies and never routes orders.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.bhavcopy_store import BhavcopyStore
from core.edge_research_ledger import deflated_sharpe
from core.earnings_calendar import events_for
from core.eod_options_backtest import EODOptionsBacktest, walk_forward


TOP30_FNO = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "LT", "SBIN",
    "BHARTIARTL", "AXISBANK", "KOTAKBANK", "ITC", "HINDUNILVR",
    "BAJFINANCE", "M&M", "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO",
    "ASIANPAINT", "NTPC", "POWERGRID", "TATASTEEL", "ADANIENT",
    "ADANIPORTS", "ONGC", "COALINDIA", "WIPRO", "HCLTECH", "TECHM",
    "BAJAJFINSV",
]


def _parse_day(raw: str) -> datetime.date:
    return datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()


def _prev_trading_day(days: List[str], event_day: str) -> Optional[str]:
    before = [d for d in days if d < event_day]
    return before[-1] if before else None


def _next_trading_day(days: List[str], event_day: str) -> Optional[str]:
    after = [d for d in days if d > event_day]
    return after[0] if after else None


def _expiry_week_blocked(store: BhavcopyStore, symbol: str, entry_day: str, event_day: str) -> Optional[str]:
    for expiry in store.expiries(symbol, entry_day):
        try:
            gap = (_parse_day(expiry) - _parse_day(event_day)).days
        except ValueError:
            continue
        if gap == 0:
            return "event_is_expiry"
        if 0 <= gap <= 6:
            return "event_in_expiry_week"
    return None


def build_event_signals(
    *,
    symbol: str,
    store: Optional[BhavcopyStore] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    event_loader: Callable[[str, str, Optional[str]], Iterable[Dict[str, Any]]] = events_for,
) -> Dict[str, Any]:
    store = store or BhavcopyStore()
    days = store.trading_days(start, end)
    if not days:
        return {"signals": [], "audit": {"events": 0, "accepted": 0, "rejected": {"no_trading_day": 0}}}
    events = list(event_loader(symbol, start or days[0], end or days[-1]))
    signals: List[Dict[str, Any]] = []
    rejected: Dict[str, int] = {}
    for event in events:
        event_day = str(event.get("date") or "")[:10]
        if not event_day:
            rejected["missing_event_date"] = rejected.get("missing_event_date", 0) + 1
            continue
        entry_day = _prev_trading_day(days, event_day)
        exit_day = _next_trading_day(days, event_day)
        if not entry_day or not exit_day:
            rejected["no_t_minus_1_or_t_plus_1"] = rejected.get("no_t_minus_1_or_t_plus_1", 0) + 1
            continue
        block = _expiry_week_blocked(store, symbol, entry_day, event_day)
        if block:
            rejected[block] = rejected.get(block, 0) + 1
            continue
        signals.append({
            "date": f"{entry_day} 11:00",
            "action": "SELL",
            "direction": "SHORT_VOL",
            "event_date": event_day,
            "planned_exit_date": exit_day,
            "symbol": symbol.upper(),
            "event_type": event.get("event_type") or "earnings",
        })
    return {
        "signals": signals,
        "audit": {
            "symbol": symbol.upper(),
            "events": len(events),
            "accepted": len(signals),
            "rejected": rejected,
            "rules": ["entry=T-1", "exit=T+1", "skip_event_expiry", "skip_expiry_week"],
        },
    }


def strategy_config(symbol: str, *, structure: str = "iron_condor",
                    short_otm_pct: float = 0.08, wing_width: int = 2) -> Dict[str, Any]:
    return {
        "id": f"S1-EARN-IV-{symbol.upper()}",
        "name": f"S1 Earnings IV Crush {symbol.upper()}",
        "python_code": "def run(data):\n    return []\n",
        "visual_config": {
            "symbol": symbol.upper(),
            "options": {
                "underlying": symbol.upper(),
                "structure": structure,
                "short_otm_pct": short_otm_pct,
                "wing_width": wing_width,
                "spread_width": wing_width,
                "exit_mode": "",
            },
        },
    }


def cost_floor_gate(overall: Dict[str, Any], *, structure: str, brokerage_per_leg: float = 40.0) -> Dict[str, Any]:
    legs = 4 if structure == "iron_condor" else 2
    round_trip_friction = brokerage_per_leg * legs * 2
    required = round_trip_friction * 3
    edge = float(overall.get("expectancy") or 0.0)
    return {
        "passed": edge >= required,
        "expected_edge_per_trade": round(edge, 2),
        "round_trip_friction": round(round_trip_friction, 2),
        "required_floor": round(required, 2),
        "multiple": round(edge / round_trip_friction, 2) if round_trip_friction else None,
    }


def grade_symbol(
    symbol: str,
    *,
    store: Optional[BhavcopyStore] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    structure: str = "iron_condor",
    short_otm_pct: float = 0.08,
    wing_width: int = 2,
    min_dte: int = 8,
    max_dte: int = 45,
    event_loader: Callable[[str, str, Optional[str]], Iterable[Dict[str, Any]]] = events_for,
) -> Dict[str, Any]:
    store = store or BhavcopyStore()
    sig = build_event_signals(symbol=symbol, store=store, start=start, end=end, event_loader=event_loader)
    cfg = strategy_config(symbol, structure=structure, short_otm_pct=short_otm_pct, wing_width=wing_width)
    params = {
        "min_dte": min_dte,
        "max_dte": max_dte,
        "max_hold_days": 2,
        "force_time_exit_days": 2,
        "short_otm_pct": short_otm_pct,
        "wing_width": wing_width,
        "width": wing_width,
    }
    res = EODOptionsBacktest(store).run(cfg, start=start, end=end, params=params, signals=sig["signals"])
    if res.get("error"):
        return {"symbol": symbol.upper(), "status": "error", "error": res["error"], "selector": sig["audit"]}
    wf = walk_forward(res)
    overall = wf.get("overall") or {}
    dsr = deflated_sharpe({
        "n": overall.get("n"),
        "expectancy": overall.get("expectancy"),
        "oos_expectancy": (wf.get("oos") or {}).get("expectancy"),
        "pct_green_months": wf.get("pct_green_months"),
    }, trials_tested=1)
    floor = cost_floor_gate(overall, structure=structure)
    paper_gate = {
        "eligible_for_paper": bool(
            overall.get("n", 0) >= 300
            and wf.get("verdict") == "CANDIDATE_EDGE"
            and dsr.get("passed")
            and floor.get("passed")
        ),
        "sample_gate": {"passed": overall.get("n", 0) >= 300, "required_n": 300},
        "deflated_sharpe": dsr,
        "cost_floor": floor,
    }
    return {
        "symbol": symbol.upper(),
        "status": "ready",
        "strategy": cfg,
        "selector": sig["audit"],
        "verdict": wf.get("verdict"),
        "overall": overall,
        "oos": wf.get("oos"),
        "oos_year": wf.get("oos_year"),
        "pct_green_months": wf.get("pct_green_months"),
        "paper_gate": paper_gate,
        "trades": res.get("trades") or [],
    }


def grade_pooled(rows: List[Dict[str, Any]], *, structure: str = "iron_condor") -> Dict[str, Any]:
    """Grade the earnings sleeve as ONE breadth strategy: pool every name's earnings
    trades into a single series and judge THAT (ERP §20 breadth law, IR = IC·√BR).

    The per-symbol gate (n>=300 PER name) is structurally impossible — a stock has
    ~10 earnings events over 2.5 years — so the flagship can only ever pass pooled,
    where n>=300 = ~120 names × a few events each. This is the honest test of the
    sleeve; the per-symbol table is only diagnostics.
    """
    trades: List[Dict[str, Any]] = []
    for r in rows:
        if r.get("status") == "ready":
            trades.extend(r.get("trades") or [])
    if not trades:
        return {"n": 0, "verdict": "INSUFFICIENT_DATA", "eligible_for_paper": False,
                "reason": "no pooled trades across the universe"}
    wf = walk_forward({"trades": trades})
    overall = wf.get("overall") or {}
    dsr = deflated_sharpe({
        "n": overall.get("n"),
        "expectancy": overall.get("expectancy"),
        "oos_expectancy": (wf.get("oos") or {}).get("expectancy"),
        "pct_green_months": wf.get("pct_green_months"),
    }, trials_tested=len(rows))     # honest multiple-testing count = names tried
    floor = cost_floor_gate(overall, structure=structure)
    eligible = bool(overall.get("n", 0) >= 300
                    and wf.get("verdict") == "CANDIDATE_EDGE"
                    and dsr.get("passed") and floor.get("passed"))
    return {
        "n": overall.get("n", 0), "verdict": wf.get("verdict"),
        "expectancy": overall.get("expectancy"), "t_stat_hac": overall.get("t_stat_hac"),
        "oos": wf.get("oos"), "pct_green_months": wf.get("pct_green_months"),
        "deflated_sharpe": dsr, "cost_floor": floor,
        "sample_gate": {"passed": overall.get("n", 0) >= 300, "required_n": 300},
        "eligible_for_paper": eligible,
    }


def grade_universe(symbols: Optional[List[str]] = None, **kwargs: Any) -> Dict[str, Any]:
    structure = kwargs.get("structure", "iron_condor")
    if kwargs.get("store") is None:
        kwargs["store"] = BhavcopyStore()
    rows = [grade_symbol(sym, **kwargs) for sym in (symbols or TOP30_FNO)]
    ready = [r for r in rows if (r.get("paper_gate") or {}).get("eligible_for_paper")]
    pooled = grade_pooled(rows, structure=structure)
    return {"count": len(rows), "eligible_for_paper": len(ready),
            "pooled": pooled, "rows": rows}
