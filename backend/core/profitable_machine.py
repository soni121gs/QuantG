"""Read-only roadmap for turning QuantG into an evidence-led trading machine.

This module is deliberately non-mutating. It joins the existing research,
execution-quality, promotion, and governance surfaces into one product map so
Hermes and the Founder UI can explain what is built, what is missing, and what
must never be bypassed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


PROGRAM_ITEMS: List[Dict[str, Any]] = [
    {
        "id": "PM-1",
        "title": "Alpha Factory",
        "stage": "partial",
        "why": "Generate many independent hypotheses, then let deterministic judges reject weak ideas quickly.",
        "current_quantg_surface": "research_hypotheses, Edge Lab, HIRB research desk, VPS research jobs",
        "next_build": "Batch hypothesis runner with checkpointed trials, candidate families, and automatic ledger evidence.",
        "hard_gate": "No paper wake unless OOS expectancy, sample size, HAC t-stat, DSR, and cost floor pass.",
    },
    {
        "id": "PM-2",
        "title": "Purged OOS Validation",
        "stage": "needed",
        "why": "Walk-forward tests still need stronger leakage protection for overlapping option holds and adaptive research.",
        "current_quantg_surface": "EOD/intraday OOS, HAC t-stat, DSR, trials count",
        "next_build": "Purged and embargoed splits plus PBO reporting for high-trial research campaigns.",
        "hard_gate": "Reject candidates that only pass ordinary splits or fail after embargo.",
    },
    {
        "id": "PM-3",
        "title": "Execution Quality Ledger",
        "stage": "needed",
        "why": "Paper edge dies when fills, spread width, slippage, and latency consume the expected profit.",
        "current_quantg_surface": "orders, trade_fills, spread_lifecycle modeled slippage, profit-giveback lab",
        "next_build": "Persist expected mid, bid/ask proxy, fill price, fill delay, adverse slippage, and missed-fill reason.",
        "hard_gate": "A strategy cannot graduate if executable net edge is materially below research net edge.",
    },
    {
        "id": "PM-4",
        "title": "ML Setup Ranker",
        "stage": "needed",
        "why": "ML should rank setups and risk, not directly place trades.",
        "current_quantg_surface": "contract_edge_score, score-IC, alpha-beta, attribution rows",
        "next_build": "Train calibrated tabular models for TP-before-SL probability, expected P&L, drawdown risk, and time-to-target.",
        "hard_gate": "ML predictions must show positive out-of-sample IC after costs before they affect sizing.",
    },
    {
        "id": "PM-5",
        "title": "Execution Optimizer",
        "stage": "needed",
        "why": "Modern RL is more useful for order placement and leg timing than for inventing alpha.",
        "current_quantg_surface": "live_spread_executor, live_entry_preflight, order audit rows",
        "next_build": "Simulated market-vs-limit policy, leg sequencing telemetry, and fail-closed execution recommendations.",
        "hard_gate": "Optimizer starts observe-only; live order behavior changes require founder approval and replay proof.",
    },
    {
        "id": "PM-6",
        "title": "Strategy Graduation Board",
        "stage": "partial",
        "why": "Every strategy needs one visible truth state from idea to live-ready, with blockers shown plainly.",
        "current_quantg_surface": "promotion_dashboard, strategy_governor, strategy_dossier",
        "next_build": "Add execution-quality, purged-OOS, and compliance blockers to the existing promotion ladder.",
        "hard_gate": "Founder approval plus deployment proof remains mandatory before live capital.",
    },
    {
        "id": "PM-7",
        "title": "Breadth Expansion",
        "stage": "partial",
        "why": "More independent bets are more valuable than more tuning of one NIFTY/SENSEX spread shape.",
        "current_quantg_surface": "full F&O store, earnings store, participant OI, IV surface, phase3 sleeves",
        "next_build": "Parallel candidate families across stock F&O, events, regimes, overnight gaps, and sector dispersion.",
        "hard_gate": "Each family needs its own null, data coverage proof, OOS score, and forward-paper window.",
    },
    {
        "id": "PM-8",
        "title": "LLM Research Analyst",
        "stage": "partial",
        "why": "LLMs are useful for reading, critiquing, and drafting hypotheses; they are unsafe as judges.",
        "current_quantg_surface": "Hermes read-only tools, RAG, HIRB critic, frozen evidence",
        "next_build": "Weekly synthesis that creates ledger cards with frozen sources and explicit falsification tests.",
        "hard_gate": "LLM text never promotes, trades, edits strategy config, or overrides computed evidence.",
    },
    {
        "id": "PM-9",
        "title": "Regulatory And Audit Readiness",
        "stage": "partial",
        "why": "SEBI retail-algo rules make traceability, approvals, order tags, and audit logs part of trading quality.",
        "current_quantg_surface": "live_safety_firewall, order idempotency, agent_tool_audit, risk_events",
        "next_build": "Map every live candidate to strategy ID, approval event, algo/order tags, and immutable decision evidence.",
        "hard_gate": "Live remains disabled unless broker token, arm state, reconciliation, audit trail, and founder gate all pass.",
    },
]


def _counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in rows:
        key = str(row.get("stage") or "unknown")
        out[key] = out.get(key, 0) + 1
    return out


async def build_profitable_machine_blueprint(
    db: Any,
    user_id: str,
    *,
    days: int = 30,
    include_live_flags: bool = True,
) -> Dict[str, Any]:
    """Join current QuantG evidence with the 1-9 profitable-machine roadmap."""

    days = max(1, min(int(days or 30), 180))
    from core.knowledge_layer import build_profit_giveback_lab
    from core.strategy_governor import build_strategy_governor_report

    governor = await build_strategy_governor_report(db, user_id, days=days)
    giveback = await build_profit_giveback_lab(db, user_id, days=days)
    hypotheses = await db.research_hypotheses.find(
        {"user_id": user_id},
        {"_id": 0, "hypothesis_id": 1, "status": 1, "verdict": 1, "updated_at": 1},
    ).sort("updated_at", -1).to_list(100)
    open_findings = await db.hermes_findings.find(
        {"user_id": user_id, "status": "open"},
        {"_id": 0, "probe_id": 1, "domain": 1, "severity": 1, "title": 1, "last_seen": 1},
    ).sort("last_seen", -1).to_list(50)
    alpha_beta = await db.alpha_beta_runs.find_one({}, {"_id": 0}, sort=[("generated_at", -1)])
    score_ic = await db.score_ic_runs.find_one({}, {"_id": 0}, sort=[("generated_at", -1)])

    live_flags: Optional[Dict[str, Any]] = None
    if include_live_flags:
        import os
        live_flags = {
            "CORE_ENGINE_LIVE_ENABLED": os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true",
            "LIVE_SPREADS_ENABLED": os.environ.get("LIVE_SPREADS_ENABLED", "false").lower() == "true",
            "HERMES_ADVICE_ENABLED": os.environ.get("HERMES_ADVICE_ENABLED", "false").lower() == "true",
        }

    blockers = [
        "No strategy should scale without positive net OOS expectancy, sample size, HAC t-stat, DSR, and cost-floor evidence.",
        "ML and LLM outputs remain observe-only until their predictions show positive out-of-sample IC after costs.",
        "Execution-quality data is still the biggest missing proof before paper edge can be trusted for live.",
        "Live capital requires founder approval, broker readiness, reconciliation, audit trail, and deployed commit proof.",
    ]
    if giveback.get("summary", {}).get("green_then_loss", 0) > 0:
        blockers.append("Recent profit-giveback evidence must be replayed before adding capital to affected strategies.")
    if any((row.get("governor") or {}).get("label") in {"pause", "kill_candidate"} for row in governor.get("strategies", [])):
        blockers.append("Some strategy rows are still governor pause/kill candidates in the selected window.")

    return {
        "kind": "profitable_machine_blueprint",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days,
        "headline": "Make QuantG a strict edge factory first: broad ideas, leak-proof validation, execution-quality proof, then founder-gated live.",
        "program": PROGRAM_ITEMS,
        "summary": {
            "program_items": len(PROGRAM_ITEMS),
            "stage_counts": _counts(PROGRAM_ITEMS),
            "research_hypotheses": len(hypotheses),
            "open_hermes_findings": len(open_findings),
            "strategy_governor": governor.get("summary"),
            "profit_giveback": giveback.get("summary"),
        },
        "evidence": {
            "latest_alpha_beta": alpha_beta,
            "latest_score_ic": score_ic,
            "recent_hypotheses": hypotheses[:12],
            "open_findings": open_findings[:12],
        },
        "live_flags": live_flags,
        "blockers": blockers,
        "sources": [
            "QuantG TASKS/CLAUDE laws: OOS-first, cost floor, breadth, overfitting, LLM narrates/code computes.",
            "External research reviewed 2026-09-04: Deflated Sharpe/backtest overfitting, LOB forecasting, RL execution, SEBI retail algo circular.",
        ],
        "note": "Read-only blueprint. This endpoint does not trade, wake strategies, edit config, or enable live execution.",
    }
