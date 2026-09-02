"""Read-only QuantG knowledge layer for Hermes, wiki, and strategy governance.

This module joins strategy config, live/paper trade evidence, attribution,
governor labels, and wiki notes into deterministic records. It never mutates
orders, broker state, strategy status, or live flags.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.strategy_governor import build_strategy_governor_report
from core.trade_attribution import attribution_rollup, compile_trade_attribution


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_ist(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _regex_words(text: str) -> List[Dict[str, Any]]:
    words = [w for w in re.split(r"\W+", str(text or "")) if len(w) > 2]
    clauses = []
    for word in words[:8]:
        esc = re.escape(word)
        clauses.append({
            "$or": [
                {"title": {"$regex": esc, "$options": "i"}},
                {"topic": {"$regex": esc, "$options": "i"}},
                {"content": {"$regex": esc, "$options": "i"}},
                {"tags": {"$regex": esc, "$options": "i"}},
            ]
        })
    return clauses


def promotion_stage(governor_label: str, oos_status: Optional[str], forward_n: int, forward_pnl: float) -> Dict[str, Any]:
    """Truthful strategy ladder: idea -> OOS -> forward-paper -> live candidate."""
    status = str(oos_status or "").upper()
    label = str(governor_label or "observe")
    blockers: List[str] = []

    if status and status not in {"CANDIDATE_EDGE", "PASS", "HISTORICAL_PASS"}:
        blockers.append(f"OOS verdict is {status}, not a pass")
    if forward_n < 30:
        blockers.append(f"forward-paper sample too thin: {forward_n} closes (<30)")
    if forward_pnl <= 0:
        blockers.append(f"forward-paper P&L is not positive: {forward_pnl:.2f}")
    if label in {"pause", "kill_candidate"}:
        blockers.append(f"governor label is {label}")

    if label == "kill_candidate":
        stage = "kill_candidate"
    elif label == "pause":
        stage = "paused_for_review"
    elif blockers:
        stage = "forward_paper"
    elif label == "scale_candidate":
        stage = "candidate_live"
    else:
        stage = "limited_paper"
    return {
        "stage": stage,
        "governor_label": label,
        "oos_status": status or None,
        "forward_closes": forward_n,
        "forward_pnl": round(forward_pnl, 2),
        "blockers": blockers,
        "note": "Read-only ladder. Founder approval and deploy proof are required before live capital.",
    }


def _closed_position_pnl(pos: Dict[str, Any]) -> float:
    return _f(pos.get("realized_pnl") if pos.get("realized_pnl") is not None else pos.get("pnl"))


def _position_in_window(pos: Dict[str, Any], since_iso: str, since_dt: datetime) -> bool:
    for key in ("closed_at", "updated_at", "created_at"):
        raw = pos.get(key)
        if raw is None:
            continue
        if isinstance(raw, datetime):
            if raw >= since_dt:
                return True
            continue
        if str(raw) >= since_iso:
            return True
    return False


async def build_profit_giveback_lab(db: Any, user_id: str, *, days: int = 30) -> Dict[str, Any]:
    """Rank avoidable open-profit giveback from closed strategy positions.

    This is a founder-facing version of the raw profit-giveback endpoint: it
    returns the biggest strategy/exit leaks and one plain recommended action for
    the next review. Read-only; it does not change exits or strategy state.
    """
    days = max(1, min(int(days or 30), 120))
    since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since_dt.isoformat()
    positions = await db.strategy_positions.find(
        {
            "user_id": user_id,
            "status": {"$in": ["CLOSED", "EXITED", "CANCELLED"]},
            "$or": [
                {"closed_at": {"$gte": since_iso}},
                {"updated_at": {"$gte": since_iso}},
                {"created_at": {"$gte": since_iso}},
                {"closed_at": {"$gte": since_dt}},
                {"updated_at": {"$gte": since_dt}},
                {"created_at": {"$gte": since_dt}},
            ],
        },
        {"_id": 0, "user_id": 0},
    ).to_list(5000)
    positions = [
        p for p in positions
        if p.get("status") in {"CLOSED", "EXITED", "CANCELLED"}
        and _position_in_window(p, since_iso, since_dt)
    ]

    by_strategy: Dict[str, Dict[str, Any]] = {}
    by_exit: Dict[str, Dict[str, Any]] = {}
    worst: List[Dict[str, Any]] = []
    total_closed = len(positions)
    losers = 0
    green_then_loss = 0
    loss_after_peak = 0.0
    peak_available = 0.0

    for p in positions:
        pnl = _closed_position_pnl(p)
        peak = _f(p.get("peak_pnl"))
        sid = str(p.get("strategy_id") or "UNKNOWN")
        reason = str(p.get("exit_reason") or "unknown")
        if pnl < 0:
            losers += 1
        if pnl < 0 and peak > 0:
            giveback = peak - pnl
            green_then_loss += 1
            loss_after_peak += giveback
            peak_available += peak
            for key, bucket in (
                (sid, by_strategy.setdefault(sid, {"strategy_id": sid, "green_then_loss": 0, "loss_after_peak": 0.0, "peak_available": 0.0, "losers": 0})),
                (reason, by_exit.setdefault(reason, {"exit_reason": reason, "green_then_loss": 0, "loss_after_peak": 0.0, "peak_available": 0.0, "losers": 0})),
            ):
                bucket["green_then_loss"] += 1
                bucket["loss_after_peak"] += giveback
                bucket["peak_available"] += peak
        if pnl < 0:
            by_strategy.setdefault(sid, {"strategy_id": sid, "green_then_loss": 0, "loss_after_peak": 0.0, "peak_available": 0.0, "losers": 0})["losers"] += 1
            by_exit.setdefault(reason, {"exit_reason": reason, "green_then_loss": 0, "loss_after_peak": 0.0, "peak_available": 0.0, "losers": 0})["losers"] += 1
        if pnl < 0 and peak > 0:
            worst.append({
                "position_id": p.get("id"),
                "strategy_id": sid,
                "target_symbol": p.get("target_symbol") or p.get("symbol"),
                "exit_reason": reason,
                "peak_pnl": round(peak, 2),
                "realized_pnl": round(pnl, 2),
                "profit_given_back": round(peak - pnl, 2),
                "closed_at": p.get("closed_at"),
            })

    def _finish(rows: List[Dict[str, Any]], id_key: str) -> List[Dict[str, Any]]:
        out = []
        for row in rows:
            r = dict(row)
            r["loss_after_peak"] = round(_f(r.get("loss_after_peak")), 2)
            r["peak_available"] = round(_f(r.get("peak_available")), 2)
            r["pct_losing_trades_green_first"] = round(
                r["green_then_loss"] / max(1, int(r.get("losers") or 0)), 3
            )
            if r["green_then_loss"] >= 3 or r["loss_after_peak"] >= 1000:
                r["recommended_action"] = (
                    "Replay tighter profit protection / no-progress exit before allowing this bucket to scale."
                )
            else:
                r["recommended_action"] = "Keep observing; evidence is still thin."
            out.append(r)
        out.sort(key=lambda x: x["loss_after_peak"], reverse=True)
        return out

    worst.sort(key=lambda x: x["profit_given_back"], reverse=True)
    top_strategy = _finish(list(by_strategy.values()), "strategy_id")[:1]
    action = "No urgent giveback action; sample is thin or clean."
    if top_strategy:
        lead = top_strategy[0]
        action = (
            f"Start exit replay on {lead['strategy_id']}: "
            f"{lead['green_then_loss']} green-then-red losers gave back Rs{lead['loss_after_peak']:,.0f}."
        )
    return {
        "kind": "profit_giveback_lab",
        "days": days,
        "since": since_iso,
        "summary": {
            "closed_trades": total_closed,
            "losers": losers,
            "green_then_loss": green_then_loss,
            "pct_losers_green_first": round(green_then_loss / max(1, losers), 3),
            "peak_profit_available": round(peak_available, 2),
            "loss_after_peak": round(loss_after_peak, 2),
        },
        "by_strategy": _finish(list(by_strategy.values()), "strategy_id")[:25],
        "by_exit_reason": _finish(list(by_exit.values()), "exit_reason")[:25],
        "worst_trades": worst[:25],
        "next_action": action,
        "note": "Read-only lab. It identifies exit leaks; any strategy/config change remains founder-approved.",
    }


async def build_daily_founder_brief(db: Any, user_id: str, *, days: int = 30,
                                    date: Optional[str] = None,
                                    persist: bool = False) -> Dict[str, Any]:
    """One decision-focused brief for the founder.

    Joins daily learning, strategy governor, profit-giveback lab, Hermes findings,
    stale-data findings, and research hypotheses into a compact action list.
    """
    date = date or _date_ist(0)
    learning = await build_daily_learning_report(db, user_id, date=date, persist=False)
    governor = await build_strategy_governor_report(db, user_id, days=days)
    giveback = await build_profit_giveback_lab(db, user_id, days=days)
    findings = await db.hermes_findings.find(
        {"user_id": user_id, "status": "open"},
        {"_id": 0, "user_id": 0},
    ).sort("last_seen", -1).to_list(50)
    hypotheses = await db.research_hypotheses.find(
        {"user_id": user_id},
        {"_id": 0, "hypothesis_id": 1, "hypothesis": 1, "verdict": 1, "status": 1, "updated_at": 1},
    ).sort("updated_at", -1).to_list(10)

    actions: List[Dict[str, Any]] = []
    if giveback["summary"]["green_then_loss"] > 0:
        actions.append({
            "priority": 1,
            "theme": "profit_giveback",
            "title": "Profit is still being left on the table",
            "why": (
                f"{giveback['summary']['green_then_loss']} losing trades were green first "
                f"and gave back Rs{giveback['summary']['loss_after_peak']:,.0f} in the last {days} days."
            ),
            "recommended_action": giveback["next_action"],
            "expected_benefit": "Lower drawdown by converting available open profit into booked profit earlier.",
        })
    stale = [f for f in findings if f.get("domain") == "data" or "stale" in str(f.get("title") or "").lower()]
    for f in stale[:2]:
        actions.append({
            "priority": 2,
            "theme": "data_freshness",
            "title": f.get("title"),
            "why": "Research/event filters are only as good as their newest store data.",
            "recommended_action": f.get("suggested_fix") or "Refresh the stale data store and rerun diagnostics.",
            "expected_benefit": "Cleaner event-risk and OOS decisions; fewer trades based on outdated context.",
        })
    regime = [f for f in findings if f.get("probe_id") == "exec.regime_organ_disagreement"]
    for f in regime[:1]:
        actions.append({
            "priority": 3,
            "theme": "regime_disagreement",
            "title": f.get("title"),
            "why": "Coarse and fine regime labels disagree often enough that one classifier may be noisy.",
            "recommended_action": "Run a regime replay against fresh broker bars before changing router logic.",
            "expected_benefit": "Prevents false stand-downs and false entries from regime-label noise.",
        })
    for row in governor.get("strategies", []):
        label = (row.get("governor") or {}).get("label")
        if label in {"pause", "kill_candidate"}:
            actions.append({
                "priority": 4,
                "theme": "strategy_governor",
                "title": f"{row.get('name') or row.get('strategy_id')} should be reviewed",
                "why": "; ".join((row.get("governor") or {}).get("reasons") or []) or f"{row.get('pnl')} P&L over {row.get('closed')} closes.",
                "recommended_action": f"Review/pause {row.get('strategy_id')} before adding capital or more trades.",
                "expected_benefit": "Stops negative-expectancy paper strategies from polluting the book and the learning set.",
            })

    actions.sort(key=lambda r: r["priority"])
    brief = {
        "kind": "daily_founder_brief",
        "date": date,
        "days": days,
        "generated_at": _iso_now(),
        "headline": (
            "Paper book is evidence-collection first; act on exit leaks, stale data, and weak strategy rows before any scaling."
        ),
        "today": {
            "realized_pnl": learning.get("realized_pnl"),
            "closed_trades": learning.get("closed_trades"),
            "open_positions": learning.get("open_positions"),
            "green_then_red_count": learning.get("green_then_red_count"),
        },
        "strategy_governor_summary": governor.get("summary"),
        "profit_giveback": giveback.get("summary"),
        "open_findings": findings[:10],
        "research_hypotheses": hypotheses,
        "recommended_actions": actions[:8],
        "note": "Read-only founder brief. It recommends work; it does not trade, pause, scale, or edit config.",
    }
    if persist:
        await db.daily_founder_briefs.update_one(
            {"user_id": user_id, "date": date},
            {"$set": {**brief, "user_id": user_id}},
            upsert=True,
        )
    return brief


async def search_wiki_knowledge(db: Any, user_id: str, query: str, *, limit: int = 12) -> Dict[str, Any]:
    match: Dict[str, Any] = {"user_id": user_id}
    clauses = _regex_words(query)
    if clauses:
        match["$and"] = clauses
    rows = await db.wiki_docs.find(
        match,
        {"_id": 0, "title": 1, "topic": 1, "tags": 1, "content": 1, "links": 1, "backlinks": 1},
    ).to_list(max(1, min(limit, 30)))
    out = []
    for row in rows:
        content = str(row.get("content") or "")
        out.append({
            "title": row.get("title"),
            "topic": row.get("topic"),
            "tags": row.get("tags") or [],
            "links": row.get("links") or [],
            "backlinks": row.get("backlinks") or [],
            "excerpt": content[:700],
        })
    return {
        "kind": "wiki_knowledge_search",
        "query": query,
        "count": len(out),
        "notes": out,
        "warning": "Wiki notes are context and decisions. Trading truth still comes from DB fills, positions, OOS validators, and risk gates.",
    }


async def build_strategy_dossier(db: Any, user_id: str, strategy_id: str, *, days: int = 30) -> Dict[str, Any]:
    days = max(1, min(int(days or 30), 180))
    strategy = await db.strategies.find_one(
        {"user_id": user_id, "id": strategy_id},
        {"_id": 0, "python_code": 0, "user_id": 0},
    ) or {"id": strategy_id, "name": strategy_id}

    gov = await build_strategy_governor_report(db, user_id, days=days)
    gov_row = next((r for r in gov.get("strategies", []) if r.get("strategy_id") == strategy_id), None)
    since = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30) - timedelta(days=days)).strftime("%Y-%m-%d")
    attr_rows = await db.trade_attribution.find(
        {"user_id": user_id, "strategy_id": strategy_id, "date_ist": {"$gte": since}},
        {"_id": 0, "user_id": 0},
    ).sort("exit_time", -1).to_list(250)

    exits = Counter(str(r.get("exit_reason") or "unknown") for r in attr_rows)
    regimes = Counter(str(r.get("regime_at_entry") or "UNKNOWN") for r in attr_rows)
    oos = await db.edge_research_runs.find_one(
        {"$or": [{"strategy_id": strategy_id}, {"strategy": strategy_id}]},
        {"_id": 0},
        sort=[("generated_at", -1), ("created_at", -1)],
    )
    if not oos:
        oos = await db.hermes_hypothesis_tests.find_one(
            {"user_id": user_id, "$or": [{"strategy_id": strategy_id}, {"dimension": strategy_id}]},
            {"_id": 0},
            sort=[("tested_at", -1), ("created_at", -1)],
        )

    wiki_query = f"{strategy.get('name') or strategy_id} {strategy_id}"
    wiki = await search_wiki_knowledge(db, user_id, wiki_query, limit=8)
    forward_pnl = _f(gov_row.get("pnl") if gov_row else 0)
    forward_n = int(gov_row.get("closed") or 0) if gov_row else 0
    oos_status = None
    if isinstance(oos, dict):
        oos_status = oos.get("verdict") or oos.get("status") or (oos.get("overall") or {}).get("verdict")

    return {
        "kind": "strategy_dossier",
        "strategy": strategy,
        "window_days": days,
        "governor": gov_row,
        "promotion": promotion_stage((gov_row or {}).get("governor", {}).get("label", "observe"), oos_status, forward_n, forward_pnl),
        "attribution": {
            "since": since,
            "closed_rows": len(attr_rows),
            "by_exit_reason": dict(exits.most_common()),
            "by_regime": dict(regimes.most_common()),
            "recent": attr_rows[:20],
        },
        "oos": oos,
        "wiki": wiki,
        "generated_at": _iso_now(),
        "note": "Dossier is read-only evidence for review; it does not wake, pause, scale, or trade.",
    }


def strategy_dossier_markdown(dossier: Dict[str, Any]) -> str:
    strategy = dossier.get("strategy") or {}
    gov = dossier.get("governor") or {}
    label = ((gov.get("governor") or {}).get("label")) or "observe"
    promo = dossier.get("promotion") or {}
    lines = [
        "---",
        "claim_type: measured",
        f"verified: {dossier.get('generated_at')}",
        "source: QuantG knowledge layer",
        "---",
        "",
        f"# {strategy.get('name') or strategy.get('id')}",
        "",
        "## Current Verdict",
        f"- Governor: {label}",
        f"- Promotion stage: {promo.get('stage')}",
        f"- Forward-paper closes: {promo.get('forward_closes')}",
        f"- Forward-paper P&L: Rs {promo.get('forward_pnl')}",
        f"- OOS status: {promo.get('oos_status') or 'not found'}",
        "",
        "## Evidence",
        f"- Window: {dossier.get('window_days')} days",
        f"- Profit factor: {gov.get('profit_factor')}",
        f"- Win rate: {gov.get('win_rate')}",
        f"- Worst loss: Rs {gov.get('worst')}",
        f"- Green-then-loss: {gov.get('green_then_loss')}",
        f"- Exit reasons: {gov.get('exit_reasons')}",
        "",
        "## Blockers",
    ]
    blockers = promo.get("blockers") or []
    lines.extend([f"- {b}" for b in blockers] or ["- None from the read-only ladder"])
    lines.extend([
        "",
        "## Related Wiki Notes",
    ])
    for note in (dossier.get("wiki") or {}).get("notes", [])[:8]:
        lines.append(f"- [[{note.get('title')}]] ({note.get('topic')})")
    lines.extend([
        "",
        "## Safety",
        "Hermes may explain and draft actions from this dossier, but strategy promotion, pausing, sizing, live flags, and broker actions require explicit founder approval.",
    ])
    return "\n".join(lines) + "\n"


async def build_daily_learning_report(db: Any, user_id: str, *, date: Optional[str] = None, persist: bool = False) -> Dict[str, Any]:
    date = date or _date_ist(0)
    try:
        await compile_trade_attribution(db, user_id, date)
    except Exception:
        pass
    since = date
    attr = await db.trade_attribution.find(
        {"user_id": user_id, "date_ist": date},
        {"_id": 0, "user_id": 0},
    ).sort("realized_pnl", 1).to_list(2000)
    open_positions = await db.strategy_positions.find(
        {"user_id": user_id, "status": "OPEN"},
        {"_id": 0, "id": 1, "strategy_id": 1, "symbol": 1, "structure": 1, "unrealized_pnl": 1, "peak_pnl": 1},
    ).to_list(500)
    gov = await build_strategy_governor_report(db, user_id, days=30)
    total = round(sum(_f(r.get("realized_pnl")) for r in attr), 2)
    best = max(attr, key=lambda r: _f(r.get("realized_pnl")), default=None)
    worst = min(attr, key=lambda r: _f(r.get("realized_pnl")), default=None)
    green_then_red = [
        r for r in attr
        if _f(r.get("realized_pnl")) < 0 and _f(r.get("peak_pnl")) > 0
    ]
    report = {
        "kind": "daily_learning_report",
        "date": date,
        "generated_at": _iso_now(),
        "realized_pnl": total,
        "closed_trades": len(attr),
        "open_positions": len(open_positions),
        "open_unrealized_pnl": round(sum(_f(p.get("unrealized_pnl")) for p in open_positions), 2),
        "best_trade": best,
        "worst_trade": worst,
        "rollups": {
            "by_strategy": await attribution_rollup(db, user_id, since, "strategy"),
            "by_structure": await attribution_rollup(db, user_id, since, "structure"),
            "by_exit_reason": await attribution_rollup(db, user_id, since, "exit_reason"),
            "by_regime": await attribution_rollup(db, user_id, since, "regime"),
        },
        "governor_summary": gov.get("summary"),
        "governor_actions": [
            {"strategy_id": r.get("strategy_id"), "name": r.get("name"), "label": (r.get("governor") or {}).get("label"), "reasons": (r.get("governor") or {}).get("reasons")}
            for r in gov.get("strategies", [])
            if (r.get("governor") or {}).get("label") in {"pause", "kill_candidate", "scale_candidate"}
        ],
        "green_then_red_count": len(green_then_red),
        "note": "Daily learning report is read-only. It can be saved as a wiki draft, but it never changes strategy state.",
    }
    if persist:
        await db.daily_learning_reports.update_one(
            {"user_id": user_id, "date": date},
            {"$set": {**report, "user_id": user_id}},
            upsert=True,
        )
    return report
