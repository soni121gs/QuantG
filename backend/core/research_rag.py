"""IA-7 (2026-07-11): Contextual retrieval (RAG) for Hermes — index our OWN validated
history (wiki notes + Hermes lessons + OOS verdicts) into the existing embedding
store (`db.hermes_memory`) so `recall_memory` (and the future IA-6 miner) can reason
from what we've already PROVEN, not just the LLM's priors.

This is the AlphaAgent "inherit validated rationales" anti-drift fix: the #1 failure
of LLM alpha miners is drifting/re-proposing dead ideas; grounding retrieval in our
own OOS verdicts (e.g. "OI/GEX is dead on NIFTY", "buyers die 5×") stops that.

Reuses the existing path: `core.embeddings.generate_gemini_embedding` (768-dim) +
`db.hermes_memory` (the collection `recall_memory` already searches). Read-only w.r.t.
trading; idempotent via a stable `rag_key` + `content_hash` so re-runs don't re-embed
unchanged docs or duplicate rows. Skips zero-vectors (embedding API down) for retry.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.embeddings import generate_gemini_embedding

# free-tier gemini-embedding-001 rate-limits bursts (429); pace calls to stay under.
_EMBED_DELAY = float(os.environ.get("RAG_EMBED_DELAY", "1.2"))


def _default_research_root() -> Path:
    here = Path(__file__).resolve()
    for root in (
        here.parents[1] / "wiki" / "Research",
        here.parents[2] / "wiki" / "Research",
    ):
        if root.exists():
            return root
    return here.parents[2] / "wiki" / "Research"


RESEARCH_ROOT = _default_research_root()


def _repo_root() -> Path:
    configured = os.environ.get("QUANTG_REPO_ROOT")
    if configured:
        return Path(configured)
    container_root = Path("/app")
    if (container_root / "CLAUDE.md").exists():
        return container_root
    return Path(__file__).resolve().parents[2]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _is_zero(vec: Optional[List[float]]) -> bool:
    return not vec or all(v == 0 for v in vec)


async def _index_one(db, user_id: str, rag_key: str, text: str, mtype: str,
                     source_refs: Optional[List[str]] = None,
                     date: Optional[str] = None) -> str:
    """Upsert one RAG doc into hermes_memory. Returns skip|indexed|embed_fail."""
    text = (text or "").strip()
    if not text:
        return "skip"
    ch = _hash(text)
    existing = await db.hermes_memory.find_one({"user_id": user_id, "rag_key": rag_key})
    if existing and existing.get("content_hash") == ch and not _is_zero(existing.get("embedding")):
        return "skip"
    emb = await generate_gemini_embedding(text[:8000])
    if _EMBED_DELAY > 0:
        await asyncio.sleep(_EMBED_DELAY)   # pace to respect the embedding rate limit
    if _is_zero(emb):
        return "embed_fail"       # API down/quota — leave for next run
    now = datetime.now(timezone.utc)
    await db.hermes_memory.update_one(
        {"user_id": user_id, "rag_key": rag_key},
        {"$set": {"user_id": user_id, "rag_key": rag_key, "content_hash": ch,
                  "text": text, "type": mtype, "source_refs": source_refs or [],
                  "embedding": emb, "date": date or now.date().isoformat(),
                  "updated_at": now.isoformat(), "_rag": True},
         "$setOnInsert": {"created_at": now.isoformat()}},
        upsert=True)
    return "indexed"


async def reindex_all(db, user_id: str, *, wiki_limit: int = 500) -> Dict[str, Any]:
    """Index wiki + Hermes lessons + latest OOS verdicts into hermes_memory.
    Idempotent; safe to run nightly. Returns per-source counts."""
    stats: Dict[str, int] = {"indexed": 0, "skip": 0, "embed_fail": 0}
    by_source: Dict[str, int] = {}

    def _bump(result: str, src: str) -> None:
        stats[result] = stats.get(result, 0) + 1
        if result == "indexed":
            by_source[src] = by_source.get(src, 0) + 1

    # ERP Phase 4 curated research corpus (disk notes reviewed by founder/agents)
    active_research_keys = set()
    if RESEARCH_ROOT.exists():
        for path in sorted(RESEARCH_ROOT.glob("*.md"))[:wiki_limit]:
            rag_key = f"research:{path.stem}"
            active_research_keys.add(rag_key)
            text = path.read_text(encoding="utf-8")
            body = f"[RESEARCH] {path.stem}\n{text[:6000]}"
            _bump(await _index_one(db, user_id, rag_key, body, "research",
                                   source_refs=[f"wiki/Research/{path.name}"]), "research")
    if active_research_keys:
        stale = await db.hermes_memory.delete_many({
            "user_id": user_id,
            "_rag": True,
            "type": "research",
            "rag_key": {"$nin": sorted(active_research_keys)},
        })
        if getattr(stale, "deleted_count", 0):
            by_source["research_stale_deleted"] = int(stale.deleted_count)

    # Canonical operator manual. This is where the current laws, postmortems,
    # deployment caveats, and broker pitfalls live; Hermes must retrieve it as
    # manual truth, not depend on shorter wiki summaries.
    manual = Path(os.environ.get("QUANTG_MANUAL_PATH", _repo_root() / "CLAUDE.md"))
    if manual.exists():
        text = manual.read_text(encoding="utf-8", errors="replace")
        current_title = "CLAUDE.md"
        current_lines: List[str] = []
        active_manual_keys = set()

        async def _flush_manual() -> None:
            if not current_lines:
                return
            section = "\n".join(current_lines).strip()
            if not section:
                return
            rag_key = f"manual:{current_title}"
            active_manual_keys.add(rag_key)
            body = f"[MANUAL] {current_title}\n{section[:7000]}"
            _bump(await _index_one(db, user_id, rag_key, body, "manual",
                                   source_refs=[current_title]), "manual")

        for line in text.splitlines():
            if line.startswith("## "):
                await _flush_manual()
                current_title = "CLAUDE.md " + line[3:].strip()
                current_lines = [line]
            else:
                current_lines.append(line)
        await _flush_manual()
        stale = await db.hermes_memory.delete_many({
            "user_id": user_id,
            "_rag": True,
            "type": "manual",
            "rag_key": {"$nin": sorted(active_manual_keys)},
        })
        if getattr(stale, "deleted_count", 0):
            by_source["manual_stale_deleted"] = int(stale.deleted_count)
    else:
        stale = await db.hermes_memory.delete_many({
            "user_id": user_id, "_rag": True, "type": "manual",
        })
        if getattr(stale, "deleted_count", 0):
            by_source["manual_stale_deleted"] = int(stale.deleted_count)

    # Repository knowledge is owner-maintained global truth. Index it explicitly
    # as global knowledge instead of requiring a user-triggered wiki sync.
    wiki_root = _repo_root() / "wiki"
    active_knowledge_keys = set()
    if wiki_root.exists():
        for path in sorted(wiki_root.rglob("*.md")):
            if path.name.lower() == "memory.md":
                continue
            rel = path.relative_to(wiki_root).as_posix()
            if rel.startswith("Research/"):
                continue
            body_text = path.read_text(encoding="utf-8", errors="replace")
            rag_key = f"knowledge:{rel}"
            active_knowledge_keys.add(rag_key)
            body = f"[GLOBAL KNOWLEDGE] {rel}\n{body_text[:7000]}"
            _bump(await _index_one(
                db, user_id, rag_key, body, "global_knowledge", source_refs=[rel],
            ), "global_knowledge")
    stale = await db.hermes_memory.delete_many({
        "user_id": user_id,
        "_rag": True,
        "type": "global_knowledge",
        "rag_key": {"$nin": sorted(active_knowledge_keys)},
    })
    if getattr(stale, "deleted_count", 0):
        by_source["global_knowledge_stale_deleted"] = int(stale.deleted_count)

    # 1) Wiki notes (user-written domain knowledge / decisions / rules)
    async for w in db.wiki_docs.find(
            {"user_id": user_id},
            {"title": 1, "topic": 1, "content": 1, "tags": 1}).limit(wiki_limit):
        title = w.get("title") or str(w.get("_id"))
        text = (f"[WIKI] {title} (topic: {w.get('topic', 'General')})\n"
                f"{(w.get('content') or '')[:4000]}")
        _bump(await _index_one(db, user_id, f"wiki:{w['_id']}", text, "wiki",
                               source_refs=[title]), "wiki")

    # 2) Hermes lessons (self-scored, validated/decayed rules)
    active_lesson_keys = set()
    async for l in db.hermes_lessons.find({
        "user_id": user_id,
        "status": "active",
        "promotion_test.passes_multiple_testing": True,
    }):
        lesson_key = f"lesson:{l.get('dimension')}:{l.get('bucket')}"
        active_lesson_keys.add(lesson_key)
        text = (f"[LESSON] dimension={l.get('dimension')} bucket={l.get('bucket')} "
                f"status={l.get('status')} hit_rate={l.get('hit_rate')} "
                f"confidence={l.get('confidence')} :: {l.get('claim') or l.get('text') or ''}")
        _bump(await _index_one(db, user_id,
                               lesson_key,
                               text, "lesson"), "lesson")
    stale = await db.hermes_memory.delete_many({
        "user_id": user_id, "_rag": True, "type": "lesson",
        "rag_key": {"$nin": sorted(active_lesson_keys)},
    })
    if getattr(stale, "deleted_count", 0):
        by_source["lesson_stale_deleted"] = int(stale.deleted_count)

    # 3) Regime-conditional OOS verdicts (the reformed judge — our validated truth)
    run = await db.regime_oos_runs.find_one(
        {"rows": {"$exists": True}, "scope": "global"}, sort=[("generated_at", -1)])
    if run:
        for row in run.get("rows", []):
            if not row.get("verdict"):
                continue
            on = row.get("on_regime") or {}
            text = (f"[GLOBAL OOS regime-conditional] {row.get('name')} "
                    f"[{row.get('underlying')} {row.get('structure')}] owns={row.get('owns')} "
                    f"VERDICT={row.get('verdict')} on-regime n={on.get('n')} avg=₹{on.get('avg')} "
                    f"wr={on.get('wr')}%; IS={row.get('in_sample', {}).get('avg')} "
                    f"OOS={row.get('out_sample', {}).get('avg')}")
            _bump(await _index_one(db, user_id, f"oos-regime:{row.get('name')}",
                                   text, "oos", source_refs=[row.get("name")]), "oos_regime")

    # 4) Edge Lab (EOD theta OOS) latest verdicts, if present
    snap = await db.edge_lab_snapshots.find_one({"_id": f"latest:{user_id}"})
    if not snap:
        snap = await db.edge_lab_snapshots.find_one({"_id": "latest", "built_by": user_id})
    if snap:
        for row in ((snap.get("oos") or {}).get("rows") or snap.get("scorecard") or snap.get("rows") or [])[:60]:
            nm = row.get("name") or row.get("strategy")
            v = row.get("verdict")
            if not nm or not v:
                continue
            text = (f"[OOS edge-lab EOD] {nm} verdict={v} "
                    f"expectancy=₹{row.get('expectancy')} n={row.get('n') or row.get('trades')}")
            _bump(await _index_one(db, user_id, f"oos-edgelab:{nm}", text, "oos",
                                   source_refs=[nm]), "oos_edgelab")

    active_keys = set()
    async for row in db.wiki_docs.find({"user_id": user_id}, {"_id": 1}):
        active_keys.add(f"wiki:{row['_id']}")
    await db.hermes_memory.delete_many({
        "user_id": user_id, "_rag": True, "type": "wiki",
        "rag_key": {"$nin": sorted(active_keys)},
    })

    return {"stats": stats, "by_source": by_source,
            "generated_at": datetime.now(timezone.utc).isoformat()}
