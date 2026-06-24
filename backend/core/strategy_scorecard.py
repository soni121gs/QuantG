"""Per-strategy risk-adjusted scorecard from REALIZED trades (db.trades).

Answers "does this strategy actually have edge?" using real round-trip P&L —
Sharpe, Sortino, expectancy, profit-factor, drawdown — not just raw P&L. Uses
the same core/metrics math as the options backtester so realized and backtested
scores are directly comparable.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional

from core.metrics import compute_metrics, grade

logger = logging.getLogger("quantg.scorecard")

# Fixed notional base so every strategy's Sharpe/return is comparable on one scale.
SCORECARD_BASE_CAPITAL = 100_000.0
CLEAN_STATS_DEFAULT_SINCE = os.environ.get(
    "QUANTG_CLEAN_STATS_SINCE",
    "2026-06-17T18:30:00+00:00",
)

# Minimum realized trades before a KILL verdict is allowed. Below this a
# negative strategy is WATCH, not KILL — the founder rule is "fix strategies,
# don't kill on 1–2 days"; a small sample is noise, not proof of no edge.
KILL_MIN_TRADES = int(os.environ.get("SCORECARD_KILL_MIN_TRADES", "15"))


def keep_kill_verdict(m: Dict[str, Any]) -> Dict[str, str]:
    """Expectancy / Sharpe / profit-factor-driven keep-kill recommendation.

    The north-star is risk-adjusted EDGE, not win rate — a 35% win-rate trend
    follower with a 1.6 payoff is KEEP; a 60% win-rate scalper whose one fat loss
    erases the day is KILL. Returns {verdict, verdict_reason}.
      KEEP  — proven positive edge (grade A/B).
      WATCH — marginal edge, or sample too thin to judge / to kill.
      KILL  — negative edge (expectancy ≤ 0 or PF < 1) on a sufficient sample.
    """
    n = int(m.get("total_trades") or 0)
    exp = float(m.get("expectancy") or 0.0)
    pf = float(m.get("profit_factor") or 0.0)
    sharpe = float(m.get("sharpe") or 0.0)
    g = grade(m)
    if n < 5:
        return {"verdict": "WATCH", "verdict_reason": f"thin sample ({n} trades) — need ≥5 to judge"}
    if g in ("A", "B"):
        return {"verdict": "KEEP",
                "verdict_reason": f"positive edge: Sharpe {sharpe:.2f}, PF {pf:.2f}, expectancy ₹{exp:.0f}"}
    if exp <= 0 or pf < 1.0:
        if n >= KILL_MIN_TRADES:
            return {"verdict": "KILL",
                    "verdict_reason": f"negative edge over {n} trades: PF {pf:.2f}, expectancy ₹{exp:.0f}"}
        return {"verdict": "WATCH",
                "verdict_reason": f"negative so far (PF {pf:.2f}, exp ₹{exp:.0f}) but only {n}<{KILL_MIN_TRADES} trades — don't kill on noise"}
    return {"verdict": "WATCH",
            "verdict_reason": f"marginal (grade {g}): Sharpe {sharpe:.2f}, PF {pf:.2f}, expectancy ₹{exp:.0f}"}


def _trade_pnl(t: Dict[str, Any]) -> float:
    for k in ("net_pnl", "pnl", "realized_pnl", "gross_pnl"):
        v = t.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return 0.0


def _trade_time(t: Dict[str, Any]) -> str:
    return str(t.get("exit_time") or t.get("closed_at") or t.get("created_at") or t.get("entry_time") or "")


async def build_scorecard(
    db,
    user_id: Optional[str] = None,
    since_iso: Optional[str] = None,
    mode: Optional[str] = None,
    clean: bool = False,
) -> List[Dict[str, Any]]:
    """Return one risk-adjusted row per strategy that has realized trades.

    Args:
        user_id: scope to one account (None = all).
        since_iso: only trades with exit_time >= this ISO string (clean-window analysis).
        mode: 'paper' / 'live' filter, if set.
        clean: when true, default to the post-contamination baseline if since_iso
            is not supplied.
    Rows are sorted best-first by (sharpe, expectancy).
    """
    if clean and not since_iso:
        since_iso = CLEAN_STATS_DEFAULT_SINCE

    query: Dict[str, Any] = {}
    if user_id:
        query["user_id"] = user_id
    if mode:
        query["mode"] = mode
    if since_iso:
        query["$or"] = [
            {"exit_time": {"$gte": since_iso}},
            {"closed_at": {"$gte": since_iso}},
            {"created_at": {"$gte": since_iso}},
        ]

    trades = await db.trades.find(query).to_list(length=200_000)

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in trades:
        sid = t.get("strategy_id")
        if sid:
            grouped[sid].append(t)

    # strategy metadata (name / structure / status)
    meta: Dict[str, Dict[str, Any]] = {}
    smq: Dict[str, Any] = {"user_id": user_id} if user_id else {}
    async for s in db.strategies.find(smq):
        sid = s.get("id") or s.get("_id")
        vc = s.get("visual_config") or {}
        meta[str(sid)] = {
            "name": s.get("name", "?"),
            "structure": ((vc.get("options") or {}).get("structure")) or "single_leg",
            "strategy_type": s.get("strategy_type", "?"),
            "status": s.get("status", "?"),
            "underlying": (vc.get("options") or {}).get("underlying") or vc.get("symbol"),
        }

    rows: List[Dict[str, Any]] = []
    for sid, ts in grouped.items():
        ts.sort(key=_trade_time)
        pnls = [_trade_pnl(t) for t in ts]
        m = compute_metrics(pnls, starting_capital=SCORECARD_BASE_CAPITAL)
        info = meta.get(str(sid), {})
        rows.append({
            "strategy_id": sid,
            "name": info.get("name", "?"),
            "structure": info.get("structure", "?"),
            "strategy_type": info.get("strategy_type", "?"),
            "status": info.get("status", "?"),
            "underlying": info.get("underlying"),
            "stats_window": "clean" if since_iso else "lifetime",
            "since": since_iso,
            "clean_epoch": bool(since_iso),
            "grade": grade(m),
            **keep_kill_verdict(m),
            **m,
        })

    rows.sort(key=lambda r: (r["sharpe"], r["expectancy"]), reverse=True)
    return rows


def summarize_verdicts(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Portfolio roll-up of keep/kill verdicts — the at-a-glance book health."""
    counts = {"KEEP": 0, "WATCH": 0, "KILL": 0}
    kill_names: List[str] = []
    keep_names: List[str] = []
    for r in rows:
        v = r.get("verdict", "WATCH")
        counts[v] = counts.get(v, 0) + 1
        if v == "KILL":
            kill_names.append(r.get("name", "?"))
        elif v == "KEEP":
            keep_names.append(r.get("name", "?"))
    return {"counts": counts, "keep": keep_names, "kill": kill_names}


def summarize_by_structure(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Aggregate scorecard rows by structure (single_leg vs credit_spread vs
    debit_spread) — the sellers-vs-buyers comparison, pooled at trade level."""
    pooled_pnls: Dict[str, List[float]] = defaultdict(list)
    # Re-pool from equity deltas isn't available here; approximate by summing each
    # strategy's per-trade stats via totals. We rebuild pooled pnls from totals.
    agg: Dict[str, Dict[str, float]] = defaultdict(lambda: {
        "strategies": 0, "total_trades": 0, "total_pnl": 0.0, "wins": 0, "losses": 0,
    })
    for r in rows:
        st = r.get("structure") or "single_leg"
        a = agg[st]
        a["strategies"] += 1
        a["total_trades"] += r["total_trades"]
        a["total_pnl"] += r["total_pnl"]
        a["wins"] += r["wins"]
        a["losses"] += r["losses"]
    out: Dict[str, Dict[str, Any]] = {}
    for st, a in agg.items():
        tt = a["total_trades"]
        decided = a["wins"] + a["losses"]
        out[st] = {
            "strategies": int(a["strategies"]),
            "total_trades": int(tt),
            "total_pnl": round(a["total_pnl"], 2),
            "win_rate": round(a["wins"] / decided, 4) if decided else 0.0,
            "expectancy": round(a["total_pnl"] / tt, 2) if tt else 0.0,
        }
    return out
