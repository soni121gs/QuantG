from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


HYPOTHESIS_STATUSES = {
    "draft",
    "ready_for_test",
    "testing",
    "validated",
    "rejected",
    "watch",
    "decayed",
}

VERDICT_STATUSES = {
    "UNTESTED",
    "INSUFFICIENT_DATA",
    "CANDIDATE_EDGE",
    "FRAGILE",
    "NO_EDGE_NEGATIVE",
    "REJECTED",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_hash(value: Dict[str, Any]) -> str:
    text = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def _clean_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def public_research_doc(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not doc:
        return {}
    out = dict(doc)
    out.pop("_id", None)
    return out


def build_hypothesis_doc(
    payload: Dict[str, Any],
    *,
    user_id: str,
    created_by: str = "hermes",
) -> Dict[str, Any]:
    hypothesis = str(payload.get("hypothesis") or "").strip()
    if not hypothesis:
        raise ValueError("hypothesis is required")

    status = str(payload.get("status") or "draft").strip() or "draft"
    if status not in HYPOTHESIS_STATUSES:
        raise ValueError(f"invalid status: {status}")

    now = _utc_now()
    canonical = {
        "hypothesis": hypothesis,
        "market_premise": payload.get("market_premise"),
        "instrument": payload.get("instrument"),
        "timeframe": payload.get("timeframe"),
        "entry_exit_idea": payload.get("entry_exit_idea"),
        "data_window": payload.get("data_window") or {},
        "null_hypothesis": payload.get("null_hypothesis"),
        "expected_edge": payload.get("expected_edge") or {},
        "cost_model": payload.get("cost_model") or {},
        "risk_thesis": payload.get("risk_thesis"),
        "failure_modes": _clean_list(payload.get("failure_modes")),
    }

    return {
        "hypothesis_id": payload.get("hypothesis_id") or f"rh_{uuid.uuid4().hex[:16]}",
        "user_id": user_id,
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "status": status,
        **canonical,
        "evidence_links": _clean_list(payload.get("evidence_links")),
        "verdict": payload.get("verdict") or {
            "status": "UNTESTED",
            "confidence": 0.0,
            "summary": "No test evidence attached yet.",
        },
        "research_context": payload.get("research_context") or {},
        "tags": _clean_list(payload.get("tags")),
        "hypothesis_hash": _stable_hash(canonical),
    }


async def ensure_research_indexes(db) -> None:
    await db.research_hypotheses.create_index("hypothesis_id", unique=True)
    await db.research_hypotheses.create_index([("user_id", 1), ("updated_at", -1)])
    await db.research_hypotheses.create_index([("user_id", 1), ("status", 1), ("updated_at", -1)])
    await db.research_hypotheses.create_index([("user_id", 1), ("hypothesis_hash", 1)])


async def create_hypothesis(
    db,
    user_id: str,
    payload: Dict[str, Any],
    *,
    created_by: str = "hermes",
) -> Dict[str, Any]:
    await ensure_research_indexes(db)
    doc = build_hypothesis_doc(payload, user_id=user_id, created_by=created_by)
    existing = await db.research_hypotheses.find_one({
        "user_id": user_id,
        "hypothesis_hash": doc["hypothesis_hash"],
    })
    if existing:
        return {**public_research_doc(existing), "deduped": True}
    await db.research_hypotheses.insert_one(doc)
    return public_research_doc(doc)


async def list_hypotheses(
    db,
    user_id: str,
    *,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"user_id": user_id}
    if status:
        query["status"] = status
    rows = await db.research_hypotheses.find(query, {"_id": 0}).sort("updated_at", -1).to_list(max(1, min(limit, 200)))
    return rows


async def get_hypothesis(db, user_id: str, hypothesis_id: str) -> Dict[str, Any]:
    doc = await db.research_hypotheses.find_one(
        {"user_id": user_id, "hypothesis_id": hypothesis_id},
        {"_id": 0},
    )
    if not doc:
        raise KeyError(hypothesis_id)
    return doc


async def attach_evidence(
    db,
    user_id: str,
    hypothesis_id: str,
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    evidence_doc = {
        "evidence_id": evidence.get("evidence_id") or f"ev_{uuid.uuid4().hex[:16]}",
        "source": evidence.get("source") or "unknown",
        "summary": evidence.get("summary") or "",
        "metrics": evidence.get("metrics") or {},
        "sample_n": int(evidence.get("sample_n") or 0),
        "confidence": float(evidence.get("confidence") or 0.0),
        "limitations": _clean_list(evidence.get("limitations")),
        "created_at": _utc_now(),
    }
    result = await db.research_hypotheses.update_one(
        {"user_id": user_id, "hypothesis_id": hypothesis_id},
        {
            "$push": {"evidence_links": evidence_doc},
            "$set": {"updated_at": _utc_now(), "status": "testing"},
        },
    )
    if getattr(result, "matched_count", 0) == 0:
        raise KeyError(hypothesis_id)
    return await get_hypothesis(db, user_id, hypothesis_id)


async def set_verdict(
    db,
    user_id: str,
    hypothesis_id: str,
    verdict: Dict[str, Any],
) -> Dict[str, Any]:
    status = str(verdict.get("status") or "").strip()
    if status not in VERDICT_STATUSES:
        raise ValueError(f"invalid verdict status: {status}")

    verdict_doc = {
        "status": status,
        "summary": verdict.get("summary") or "",
        "confidence": float(verdict.get("confidence") or 0.0),
        "sample_n": int(verdict.get("sample_n") or 0),
        "metrics": verdict.get("metrics") or {},
        "limitations": _clean_list(verdict.get("limitations")),
        "decided_at": _utc_now(),
    }
    next_status = {
        "CANDIDATE_EDGE": "validated",
        "FRAGILE": "watch",
        "NO_EDGE_NEGATIVE": "rejected",
        "REJECTED": "rejected",
        "INSUFFICIENT_DATA": "watch",
        "UNTESTED": "draft",
    }[status]
    result = await db.research_hypotheses.update_one(
        {"user_id": user_id, "hypothesis_id": hypothesis_id},
        {"$set": {"verdict": verdict_doc, "status": next_status, "updated_at": _utc_now()}},
    )
    if getattr(result, "matched_count", 0) == 0:
        raise KeyError(hypothesis_id)
    return await get_hypothesis(db, user_id, hypothesis_id)
