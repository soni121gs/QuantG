from __future__ import annotations

from typing import Any, Dict, List


PROVEN_VERDICTS = {"CANDIDATE_EDGE"}
WEAK_VERDICTS = {"UNTESTED", "INSUFFICIENT_DATA", "FRAGILE", "NO_EDGE_NEGATIVE", "REJECTED"}


def _evidence_rows(hypothesis: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = hypothesis.get("evidence_links") or []
    return [row for row in rows if isinstance(row, dict)]


def _has_source(rows: List[Dict[str, Any]], needle: str) -> bool:
    n = needle.lower()
    return any(n in str(row.get("source") or "").lower() for row in rows)


def critique_hypothesis(hypothesis: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic research critic for HIRB.

    It does not decide trades. It decides how strong a research claim is allowed
    to sound, based on evidence links, verdict status, sample size, and known
    failure modes.
    """
    verdict = hypothesis.get("verdict") or {}
    verdict_status = str(verdict.get("status") or "UNTESTED")
    rows = _evidence_rows(hypothesis)
    sample_n = int(verdict.get("sample_n") or sum(int(row.get("sample_n") or 0) for row in rows))
    confidence = float(verdict.get("confidence") or 0.0)
    blockers: List[str] = []
    missing_data: List[str] = []
    falsification_tests: List[str] = []
    contradictions: List[str] = []

    if not rows:
        blockers.append("no evidence links attached")
    if sample_n < 30:
        blockers.append(f"sample below OOS gate: {sample_n} < 30")
    if not _has_source(rows, "oos") and "OOS" not in str(verdict.get("summary") or "").upper():
        missing_data.append("out-of-sample or walk-forward evidence")
    if not _has_source(rows, "forward") and not _has_source(rows, "paper"):
        missing_data.append("forward-paper evidence")
    if not any("slippage" in str(row.get("metrics") or "").lower() or "cost" in str(row.get("metrics") or "").lower() for row in rows):
        missing_data.append("cost/slippage stress evidence")

    for mode in hypothesis.get("failure_modes") or []:
        falsification_tests.append(f"Replay and isolate failure mode: {mode}")
    falsification_tests.extend([
        "Check whether expectancy remains positive after realistic costs.",
        "Check whether nearby parameters form a plateau instead of one lucky cell.",
        "Check whether latest forward-paper trades match historical expectancy direction.",
    ])

    if verdict_status in {"NO_EDGE_NEGATIVE", "REJECTED"}:
        contradictions.append("current verdict rejects the hypothesis")
    if verdict_status == "FRAGILE":
        contradictions.append("current verdict says edge is fragile")

    can_call_proven = (
        verdict_status in PROVEN_VERDICTS
        and sample_n >= 30
        and confidence >= 0.6
        and not contradictions
        and "out-of-sample or walk-forward evidence" not in missing_data
    )
    allowed_claim = "validated_research_candidate" if can_call_proven else "unproven_research_hypothesis"
    if verdict_status in WEAK_VERDICTS and verdict_status not in PROVEN_VERDICTS:
        allowed_claim = "not_proven"

    return {
        "hypothesis_id": hypothesis.get("hypothesis_id"),
        "plan": [
            "retrieve hypothesis and attached evidence",
            "compute evidence strength and sample-size gates",
            "challenge the claim with failure modes and missing data",
            "verify OOS/forward-paper/cost evidence before any strong wording",
            "summarize allowed claim and next test",
        ],
        "verdict_status": verdict_status,
        "allowed_claim": allowed_claim,
        "can_call_proven": can_call_proven,
        "sample_n": sample_n,
        "confidence": confidence,
        "blockers": blockers,
        "missing_data": missing_data,
        "falsification_tests": falsification_tests,
        "contradictions": contradictions,
        "summary": (
            "Research candidate has enough evidence to discuss as validated."
            if can_call_proven
            else "Do not present this as proven; more evidence or cleaner validation is required."
        ),
    }


def critique_research_answer(question: str, hypotheses: List[Dict[str, Any]]) -> Dict[str, Any]:
    critiques = [critique_hypothesis(row) for row in hypotheses]
    proven = [row for row in critiques if row["can_call_proven"]]
    blocked = [row for row in critiques if not row["can_call_proven"]]
    return {
        "question": question,
        "workflow": "plan -> retrieve -> compute -> challenge -> verify -> summarize",
        "proven_count": len(proven),
        "blocked_count": len(blocked),
        "critiques": critiques,
        "answer_rule": (
            "Only claims with can_call_proven=true may be described as validated; "
            "everything else must be labelled unproven, fragile, rejected, or insufficient-data."
        ),
    }
