"""Research bridge — auto-populate the Research Ledger from the engines that
already produce verdicts.

Two feeds converge on ONE per-strategy "edge" hypothesis row (keyed by a
deterministic id from strategy_id), so a strategy's live-loss evidence and its
OOS verdict land on the SAME hypothesis — closing the loop the founder wanted:

    Diagnostician finding (live loss / bad geometry)  ─┐
                                                        ├─► rh_edge_<strategy>
    OOS validator verdict (CANDIDATE_EDGE / …)        ─┘   (evidence + verdict + status)

Design law (same as HSI/Diagnostician): code computes every number; nothing here
trades or edits strategies. It only records research state. Status transitions are
deterministic (verdict → status), matching research_ledger.set_verdict.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.research_ledger import ensure_research_indexes, get_hypothesis

logger = logging.getLogger("quantg.research_bridge")

_ENABLED = os.environ.get("HERMES_RESEARCH_BRIDGE", "true").lower() == "true"

# OOS verdict → hypothesis lifecycle status (mirrors research_ledger.set_verdict).
_VERDICT_TO_STATUS = {
    "CANDIDATE_EDGE": "validated",
    "FRAGILE": "watch",
    "NO_EDGE_NEGATIVE": "rejected",
    "REJECTED": "rejected",
    "INSUFFICIENT_DATA": "watch",
    "UNTESTED": "draft",
}
_VALID_VERDICTS = set(_VERDICT_TO_STATUS)

# Which Diagnostician probes are EDGE questions (→ research) vs bugs (→ fix-tasks).
_RESEARCH_PROBES = {"strategy.persistent_live_loss", "static.reward_risk_geometry"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _edge_hid(strategy_id: str) -> str:
    return "rh_edge_" + hashlib.sha256(str(strategy_id).encode()).hexdigest()[:16]


async def upsert_strategy_edge(
    db, user_id: str, *, strategy_id: str, name: str,
    instrument: Optional[str] = None, structure: Optional[str] = None,
    timeframe: str = "", source: str = "bridge",
    evidence: Optional[Dict[str, Any]] = None,
    verdict: Optional[Dict[str, Any]] = None,
    entry_exit_idea: Optional[str] = None,
) -> Optional[str]:
    """Create-or-update the single edge hypothesis for a strategy. `evidence` is a
    dict pushed onto evidence_links; `verdict` is {status, summary, metrics,
    sample_n} that also advances the hypothesis status. Returns the hypothesis_id."""
    if not _ENABLED:
        return None
    await ensure_research_indexes(db)
    hid = _edge_hid(strategy_id)
    now = _utc_now()

    canonical = {
        "hypothesis": (f"{name}: does this {structure or 'strategy'}"
                       f"{(' on ' + instrument) if instrument else ''} have a positive "
                       "out-of-sample edge after realistic costs?"),
        "market_premise": "A live strategy is only worth capital if it survives OOS + forward-paper.",
        "instrument": instrument, "timeframe": timeframe,
        "entry_exit_idea": entry_exit_idea or name,
        "null_hypothesis": (f"{name} has NO positive out-of-sample edge after costs "
                            "(live loss is the strategy, not variance)."),
        "structure": structure,
    }
    set_doc: Dict[str, Any] = {"updated_at": now, "strategy_id": strategy_id,
                               "last_source": source, **canonical}
    on_insert: Dict[str, Any] = {
        "hypothesis_id": hid, "user_id": user_id, "created_by": source,
        "created_at": now, "status": "ready_for_test",
        "verdict": {"status": "UNTESTED", "confidence": 0.0,
                    "summary": "No test evidence attached yet."},
        "tags": ["auto", "edge", source],
        "research_context": {"strategy_id": strategy_id},
    }
    update: Dict[str, Any] = {"$set": set_doc, "$setOnInsert": on_insert}

    if evidence:
        # `evidence_links` is created by $push; it must NOT also appear in
        # $setOnInsert (Mongo rejects the same path in both operators).
        ev = {"evidence_id": "ev_" + hashlib.sha256(
                  (str(evidence.get("source")) + now).encode()).hexdigest()[:14],
              "source": evidence.get("source") or source,
              "summary": evidence.get("summary") or "",
              "metrics": evidence.get("metrics") or {},
              "sample_n": int(evidence.get("sample_n") or 0),
              "confidence": float(evidence.get("confidence") or 0.0),
              "created_at": now}
        update["$push"] = {"evidence_links": ev}
    else:
        # No evidence this call → seed the empty array on insert so the field
        # always exists for readers.
        on_insert["evidence_links"] = []

    if verdict and str(verdict.get("status")) in _VALID_VERDICTS:
        vstatus = str(verdict["status"])
        set_doc["verdict"] = {
            "status": vstatus, "summary": verdict.get("summary") or "",
            "confidence": float(verdict.get("confidence") or 0.0),
            "sample_n": int(verdict.get("sample_n") or 0),
            "metrics": verdict.get("metrics") or {}, "decided_at": now,
        }
        # A real verdict advances lifecycle; setOnInsert.status only applies on insert.
        set_doc["status"] = _VERDICT_TO_STATUS[vstatus]

    # Mongo forbids the same field path in $set and $setOnInsert. When a verdict
    # is present it drives status+verdict via $set, so drop those from the
    # insert-only defaults (they only exist to seed a brand-new row).
    for k in list(on_insert.keys()):
        if k in set_doc:
            on_insert.pop(k)

    await db.research_hypotheses.update_one(
        {"user_id": user_id, "hypothesis_id": hid}, update, upsert=True)
    return hid


# ── Feed 1: OOS validator (eod-options-backtest) → ledger ────────────────────

async def record_oos_result(db, user_id: str, strat: Dict[str, Any],
                            wf: Dict[str, Any], window: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Auto-write one strategy's walk-forward OOS verdict onto its edge hypothesis."""
    verdict_status = str(wf.get("verdict") or "").strip()
    if verdict_status not in _VALID_VERDICTS:
        return None
    sid = str(strat.get("id") or strat.get("strategy_id"))
    opts = ((strat.get("visual_config") or {}).get("options") or {})
    metrics = {"overall": wf.get("overall"), "oos": wf.get("oos"),
               "oos_year": wf.get("oos_year"), "pct_green_months": wf.get("pct_green_months")}
    summary = (f"EOD walk-forward OOS: {verdict_status} "
               f"(OOS {wf.get('oos')}, {wf.get('pct_green_months')}% green months).")
    return await upsert_strategy_edge(
        db, user_id, strategy_id=sid, name=strat.get("name") or sid,
        instrument=opts.get("underlying") or strat.get("underlying"),
        structure=opts.get("structure") or strat.get("structure"),
        timeframe="EOD / held-to-theta", source="eod_oos_backtest",
        evidence={"source": "eod_oos_backtest", "summary": summary, "metrics": metrics,
                  "sample_n": int(wf.get("n") or 0)},
        verdict={"status": verdict_status, "summary": summary, "metrics": metrics,
                 "sample_n": int(wf.get("n") or 0)},
    )


async def record_oos_backtest_batch(db, user_id: str, strat_wf_pairs, window=None) -> int:
    n = 0
    for strat, wf in strat_wf_pairs:
        try:
            if await record_oos_result(db, user_id, strat, wf, window):
                n += 1
        except Exception as exc:  # noqa: BLE001 — never break the backtest route
            logger.warning("research bridge oos-write failed: %s", exc)
    return n


# ── Feed 2: Diagnostician findings → ledger ──────────────────────────────────

async def sync_findings_to_research(db, user_id: str, findings: List[Dict[str, Any]]) -> int:
    """Turn edge-doubt findings (persistent loss / bad geometry) into a research
    hypothesis so they become a *tested* decision, not a lingering suspicion. Bug
    findings (RC-1/2/3, infra) are handled by fix-tasks, not here."""
    if not _ENABLED:
        return 0
    n = 0
    for f in findings:
        if f.get("probe_id") not in _RESEARCH_PROBES:
            continue
        ev = f.get("evidence") or {}
        sid = ev.get("strategy_id") or f.get("entity")
        if not sid:
            continue
        strat = await db.strategies.find_one({"$or": [{"id": sid}, {"strategy_id": sid}]},
                                             {"_id": 0, "name": 1, "visual_config": 1,
                                              "structure": 1, "underlying": 1})
        opts = ((strat or {}).get("visual_config") or {}).get("options") or {}
        try:
            await upsert_strategy_edge(
                db, user_id, strategy_id=sid,
                name=ev.get("name") or (strat or {}).get("name") or str(sid),
                instrument=opts.get("underlying") or (strat or {}).get("underlying"),
                structure=opts.get("structure") or (strat or {}).get("structure"),
                source="diagnostician",
                evidence={"source": f.get("probe_id"), "summary": f.get("title"),
                          "metrics": ev, "sample_n": int(ev.get("trades") or 0)},
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("research bridge finding-write failed: %s", exc)
    return n
