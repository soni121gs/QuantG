from __future__ import annotations

from typing import Any, Dict, List

from core.research_critic import critique_hypothesis


ROLE_SPECS = {
    "regime_analyst": "Checks whether the idea is regime-conditioned and falsifiable by market state.",
    "volatility_analyst": "Checks IV/RV, premium richness, skew, and volatility state evidence.",
    "flow_analyst": "Checks OI/volume/order-flow evidence and whether it is reconstructable historically.",
    "strategy_scientist": "Checks hypothesis clarity, null hypothesis, and testability.",
    "backtest_engineer": "Checks OOS/walk-forward/sample-size evidence and data leakage risks.",
    "risk_officer": "Checks drawdown, CVaR, concentration, Kelly, and failure modes.",
    "execution_cost_auditor": "Checks slippage/spread/cost stress and fill realism.",
    "skeptic": "Looks for contradictions, overfit, missing evidence, and weak claims.",
    "memory_curator": "Decides what can be remembered as fact vs watch item.",
    "founder_briefing_agent": "Summarizes the allowed claim and next action.",
}


def _contains(text: str, needles: List[str]) -> bool:
    low = text.lower()
    return any(n in low for n in needles)


def _evidence_text(evidence: List[Dict[str, Any]]) -> str:
    return " ".join(
        f"{row.get('source', '')} {row.get('summary', '')} {row.get('metrics', '')}"
        for row in evidence
    )


def run_research_desk(
    hypothesis: Dict[str, Any],
    *,
    evidence: List[Dict[str, Any]] | None = None,
    metrics: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    evidence_rows = evidence if evidence is not None else hypothesis.get("evidence_links") or []
    metrics = metrics or (hypothesis.get("verdict") or {}).get("metrics") or {}
    text = " ".join([
        str(hypothesis.get("hypothesis") or ""),
        str(hypothesis.get("market_premise") or ""),
        str(hypothesis.get("entry_exit_idea") or ""),
        _evidence_text(evidence_rows),
    ])
    critique = critique_hypothesis({**hypothesis, "evidence_links": evidence_rows})

    role_outputs = []
    role_outputs.append({
        "role": "regime_analyst",
        "status": "ok" if _contains(text, ["regime", "range", "trend", "stormy"]) else "missing_context",
        "finding": "Regime condition present." if _contains(text, ["regime", "range", "trend", "stormy"]) else "Add regime split before promotion.",
        "evidence_refs": [row.get("evidence_id") for row in evidence_rows if row.get("evidence_id")],
    })
    role_outputs.append({
        "role": "volatility_analyst",
        "status": "ok" if _contains(text, ["iv", "rv", "vol", "skew"]) else "missing_context",
        "finding": "Volatility premise is explicit." if _contains(text, ["iv", "rv", "vol", "skew"]) else "Add IV-RV/skew evidence.",
        "evidence_refs": [row.get("evidence_id") for row in evidence_rows if row.get("evidence_id")],
    })
    role_outputs.append({
        "role": "flow_analyst",
        "status": "watch" if _contains(text, ["oi", "flow", "volume"]) else "missing_context",
        "finding": "Flow/OI evidence present." if _contains(text, ["oi", "flow", "volume"]) else "Flow evidence absent; do not use flow as a premise.",
        "evidence_refs": [],
    })
    role_outputs.append({
        "role": "strategy_scientist",
        "status": "ok" if hypothesis.get("null_hypothesis") else "missing_context",
        "finding": "Null hypothesis is defined." if hypothesis.get("null_hypothesis") else "Define a null hypothesis before testing.",
        "evidence_refs": [],
    })
    role_outputs.append({
        "role": "backtest_engineer",
        "status": "ok" if critique["sample_n"] >= 30 and not critique["missing_data"] else "blocked",
        "finding": "OOS/sample gate is adequate." if critique["sample_n"] >= 30 and not critique["missing_data"] else "OOS, forward-paper, or sample-size evidence is incomplete.",
        "evidence_refs": [row.get("evidence_id") for row in evidence_rows if row.get("evidence_id")],
    })
    role_outputs.append({
        "role": "risk_officer",
        "status": "ok" if metrics.get("max_drawdown") is not None or metrics.get("cvar_95") is not None else "missing_context",
        "finding": "Risk metrics are available." if metrics.get("max_drawdown") is not None or metrics.get("cvar_95") is not None else "Add drawdown/CVaR/tail-risk metrics.",
        "evidence_refs": [],
    })
    role_outputs.append({
        "role": "execution_cost_auditor",
        "status": "ok" if metrics.get("slippage_stress") else "blocked",
        "finding": "Cost stress is available." if metrics.get("slippage_stress") else "Add slippage/spread stress before promotion.",
        "evidence_refs": [],
    })
    role_outputs.append({
        "role": "skeptic",
        "status": "blocked" if critique["blockers"] or critique["contradictions"] else "ok",
        "finding": "; ".join(critique["blockers"] + critique["contradictions"]) or "No hard contradiction found.",
        "evidence_refs": [],
    })
    role_outputs.append({
        "role": "memory_curator",
        "status": "fact" if critique["can_call_proven"] else "watch",
        "finding": "May be remembered as validated candidate." if critique["can_call_proven"] else "Store as watch/research item, not fact.",
        "evidence_refs": [],
    })
    role_outputs.append({
        "role": "founder_briefing_agent",
        "status": "ready" if critique["can_call_proven"] else "needs_more_work",
        "finding": critique["summary"],
        "evidence_refs": [],
    })

    return {
        "hypothesis_id": hypothesis.get("hypothesis_id"),
        "roles": ROLE_SPECS,
        "outputs": role_outputs,
        "critic": critique,
        "allowed_claim": critique["allowed_claim"],
        "next_action": "prepare founder review" if critique["can_call_proven"] else "run missing tests and attach evidence",
    }
