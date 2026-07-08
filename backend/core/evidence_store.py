from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(payload: Dict[str, Any]) -> str:
    frozen = {
        "source_type": payload.get("source_type"),
        "source_uri": payload.get("source_uri"),
        "observed_at": payload.get("observed_at"),
        "content": payload.get("content"),
        "metadata": payload.get("metadata") or {},
    }
    text = json.dumps(frozen, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_evidence_doc(payload: Dict[str, Any], *, user_id: str) -> Dict[str, Any]:
    source_type = str(payload.get("source_type") or "").strip()
    if not source_type:
        raise ValueError("source_type is required")
    content = payload.get("content")
    if content in (None, ""):
        raise ValueError("content is required")

    observed_at = payload.get("observed_at") or _utc_now()
    normalized = {
        "source_type": source_type,
        "source_uri": payload.get("source_uri"),
        "observed_at": observed_at,
        "content": content,
        "metadata": payload.get("metadata") or {},
    }
    digest = _content_hash(normalized)
    return {
        "evidence_snapshot_id": payload.get("evidence_snapshot_id") or f"fe_{uuid.uuid4().hex[:16]}",
        "user_id": user_id,
        **normalized,
        "content_hash": digest,
        "collected_at": _utc_now(),
        "valid_from": payload.get("valid_from") or observed_at,
        "valid_to": payload.get("valid_to"),
        "anti_leakage": {
            "frozen": True,
            "rule": "Use this snapshot only as of observed_at/collected_at; do not replace it with future-updated web content in historical research.",
        },
    }


def public_evidence_doc(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not doc:
        return {}
    out = dict(doc)
    out.pop("_id", None)
    return out


async def ensure_evidence_indexes(db) -> None:
    await db.frozen_evidence.create_index("evidence_snapshot_id", unique=True)
    await db.frozen_evidence.create_index([("user_id", 1), ("content_hash", 1)])
    await db.frozen_evidence.create_index([("user_id", 1), ("observed_at", -1)])
    await db.frozen_evidence.create_index([("user_id", 1), ("source_type", 1), ("observed_at", -1)])


async def freeze_evidence(db, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    await ensure_evidence_indexes(db)
    doc = build_evidence_doc(payload, user_id=user_id)
    existing = await db.frozen_evidence.find_one({
        "user_id": user_id,
        "content_hash": doc["content_hash"],
    })
    if existing:
        return {**public_evidence_doc(existing), "deduped": True}
    await db.frozen_evidence.insert_one(doc)
    return public_evidence_doc(doc)


async def get_evidence(db, user_id: str, evidence_snapshot_id: str) -> Dict[str, Any]:
    doc = await db.frozen_evidence.find_one(
        {"user_id": user_id, "evidence_snapshot_id": evidence_snapshot_id},
        {"_id": 0},
    )
    if not doc:
        raise KeyError(evidence_snapshot_id)
    return doc


async def list_evidence(
    db,
    user_id: str,
    *,
    source_type: Optional[str] = None,
    as_of: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"user_id": user_id}
    if source_type:
        query["source_type"] = source_type
    if as_of:
        query["observed_at"] = {"$lte": as_of}
    rows = await db.frozen_evidence.find(query, {"_id": 0}).sort("observed_at", -1).to_list(max(1, min(limit, 200)))
    return rows
