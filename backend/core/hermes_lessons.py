"""HSI Stage 3 — Scored Lesson Store (deterministic, no LLM).

The "memory that gets smarter" layer. Stage 1 (trade_attribution) computes the
per-dimension edge of every closed trade; Stage 2 (hermes_observations) narrates
it. Stage 3 turns those into **lessons** that are re-tested against fresh
attribution every EOD and either gain confidence, get promoted, or decay.

Design law (CLAUDE.md §12): code computes every number; the LLM never touches
this file. A lesson's direction/metric/hit-rate/confidence are all derived from
`trade_attribution.attribution_rollup` (deterministic). The human-readable
`claim` is synthesised from the same numbers — no model in the loop.

Lifecycle
---------
    candidate --(persists K days, hit_rate ok)--> active
       │                                              │
       └────────────── decayed (stale, or hit_rate too low over enough obs) ◄┘

A lesson is keyed by (dimension, bucket) — e.g. dimension="structure",
bucket="credit_spread". Each EOD:
  • the bucket's sign of expectancy today is compared to the lesson's claimed
    direction — a match is a "confirmation" (correct_count++), a mismatch is a
    "contradiction" (only observations_count++, so hit_rate falls);
  • buckets that don't trade for LESSON_DECAY_DAYS decay (stale);
  • scoring is idempotent per (lesson, date) via `last_scored_date`, so an EOD
    re-run after a restart never double-counts.

Public API
----------
- score_and_update_lessons(db, user_id, date_str) -> dict   (per-run stats)
- get_brain_health(db, user_id)                   -> dict   (HSI-34 meta-metric)
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hermes_lessons")

# ── Config (env-tunable) ─────────────────────────────────────────────────────────
# Dimensions worth turning into lessons (actionable edges). exposure_bias/regime/
# exit_reason/structure are the ones a rule could later act on.
LESSON_DIMENSIONS = ("structure", "regime", "exit_reason", "exposure_bias")
# A bucket must have at least this many trades on a day to count as one observation.
LESSON_MIN_N = int(os.environ.get("LESSON_MIN_N", "3"))
# candidate -> active once it has been confirmed this many times AND holds hit_rate.
LESSON_PROMOTE_K = int(os.environ.get("LESSON_PROMOTE_K", "3"))
LESSON_PROMOTE_HITRATE = float(os.environ.get("LESSON_PROMOTE_HITRATE", "0.6"))
# Decay: not confirmed in this many calendar days (stale), OR hit_rate below the
# floor once there are at least this many observations (wrong).
LESSON_DECAY_DAYS = int(os.environ.get("LESSON_DECAY_DAYS", "10"))
LESSON_DECAY_MIN_OBS = int(os.environ.get("LESSON_DECAY_MIN_OBS", "4"))
LESSON_DECAY_HITRATE = float(os.environ.get("LESSON_DECAY_HITRATE", "0.5"))
# Observations at which the sample factor reaches 1.0 (confidence = hit_rate·factor).
LESSON_CONF_FULL_OBS = float(os.environ.get("LESSON_CONF_FULL_OBS", "6") or 6)


# ── Pure helpers ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_between(a: str, b: str) -> int:
    """Whole days between two 'YYYY-MM-DD' strings (b - a). 0 on parse failure."""
    try:
        da = datetime.strptime(str(a)[:10], "%Y-%m-%d")
        db_ = datetime.strptime(str(b)[:10], "%Y-%m-%d")
        return (db_ - da).days
    except Exception:
        return 0


def _confidence(correct: int, observations: int) -> float:
    """Confidence = accuracy scaled by sample size. A brand-new 1/1 lesson is low
    confidence; a 6/6 lesson is 1.0; a 3/6 lesson is 0.5."""
    if observations <= 0:
        return 0.0
    hit_rate = correct / observations
    sample_factor = min(1.0, observations / LESSON_CONF_FULL_OBS)
    return round(hit_rate * sample_factor, 3)


def _claim(dimension: str, bucket: str, direction: str, expectancy: float,
           win_rate: float, n: int) -> str:
    edge = "positive" if direction == "good" else "negative"
    return (f"{dimension}={bucket}: {edge} edge "
            f"(expectancy ₹{round(expectancy)}/trade, {round((win_rate or 0) * 100)}% WR, n={n})")


def _direction_of(expectancy: float) -> Optional[str]:
    if expectancy > 0:
        return "good"
    if expectancy < 0:
        return "bad"
    return None  # exactly flat — no signal


def new_lesson(user_id: str, dimension: str, bucket: str, direction: str,
               n: int, expectancy: float, win_rate: float, date_str: str) -> Dict[str, Any]:
    """Build a fresh candidate lesson doc (pure)."""
    return {
        "lesson_id": "lsn_" + uuid.uuid4().hex[:16],
        "user_id": user_id,
        "dimension": dimension,
        "bucket": bucket,
        "claim": _claim(dimension, bucket, direction, expectancy, win_rate, n),
        "direction": direction,
        "metric": "expectancy",
        "metric_at_creation": round(expectancy, 2),
        "metric_latest": round(expectancy, 2),
        "win_rate_latest": round(win_rate, 3),
        "sample_size": n,
        "observations_count": 1,
        "correct_count": 1,
        "hit_rate": 1.0,
        "confidence": _confidence(1, 1),
        "status": "candidate",
        "created_at": _now_iso(),
        "last_confirmed_at": date_str,
        "last_scored_date": date_str,
    }


def apply_observation(lesson: Dict[str, Any], direction: str, n: int, expectancy: float,
                      win_rate: float, date_str: str) -> Dict[str, Any]:
    """Score an existing lesson against today's bucket result. Returns the `$set`
    field patch (pure — no DB). Handles confirm/contradict, promotion, and the
    low-hit-rate decay. Staleness decay is handled by the caller for buckets that
    DIDN'T trade today."""
    confirmed = (direction == lesson["direction"])
    observations = int(lesson.get("observations_count", 0)) + 1
    correct = int(lesson.get("correct_count", 0)) + (1 if confirmed else 0)
    hit_rate = round(correct / observations, 3)
    patch: Dict[str, Any] = {
        "observations_count": observations,
        "correct_count": correct,
        "hit_rate": hit_rate,
        "confidence": _confidence(correct, observations),
        "sample_size": int(lesson.get("sample_size", 0)) + n,
        "metric_latest": round(expectancy, 2),
        "win_rate_latest": round(win_rate, 3),
        "last_scored_date": date_str,
        # Refresh the human claim from the latest numbers so it never goes stale.
        "claim": _claim(lesson["dimension"], lesson["bucket"], lesson["direction"],
                        expectancy, win_rate, int(lesson.get("sample_size", 0)) + n),
    }
    if confirmed:
        patch["last_confirmed_at"] = date_str

    status = lesson.get("status", "candidate")
    if observations >= LESSON_DECAY_MIN_OBS and hit_rate < LESSON_DECAY_HITRATE:
        status = "decayed"
        patch["decay_reason"] = "low_hit_rate"
        patch["decayed_at"] = date_str
    elif status == "candidate" and correct >= LESSON_PROMOTE_K and hit_rate >= LESSON_PROMOTE_HITRATE:
        status = "active"
        patch["promoted_at"] = date_str
    patch["status"] = status
    return patch


def is_stale(lesson: Dict[str, Any], date_str: str) -> bool:
    """A live lesson whose bucket hasn't been confirmed in LESSON_DECAY_DAYS days."""
    last = lesson.get("last_confirmed_at") or lesson.get("created_at")
    if not last:
        return False
    return _days_between(str(last)[:10], date_str) > LESSON_DECAY_DAYS


# ── DB orchestration ─────────────────────────────────────────────────────────────

async def _ensure_indexes(db) -> None:
    try:
        await db.hermes_lessons.create_index([("user_id", 1), ("dimension", 1), ("bucket", 1)], unique=True)
        await db.hermes_lessons.create_index("lesson_id", unique=True)
    except Exception as exc:
        logger.warning("hermes_lessons index ensure failed: %s", exc)


async def score_and_update_lessons(db, user_id: str, date_str: str) -> Dict[str, int]:
    """Daily self-score / create / promote / decay pass. Idempotent per (lesson, date).

    Runs at EOD after Stage-1 attribution is compiled. Reads TODAY's per-dimension
    rollups (deterministic) and updates the lesson store. Returns per-run stats.
    """
    from core.trade_attribution import attribution_rollup

    await _ensure_indexes(db)
    stats = {"created": 0, "confirmed": 0, "contradicted": 0,
             "promoted": 0, "decayed": 0, "skipped_dup": 0}
    seen_keys = set()

    for dim in LESSON_DIMENSIONS:
        try:
            # since == date_str → today's IST day only.
            rollup = await attribution_rollup(db, user_id, date_str, dim)
        except Exception as exc:
            logger.error("lesson scoring rollup failed dim=%s: %s", dim, exc)
            continue

        for b in rollup:
            bucket = b.get("bucket")
            n = int(b.get("n") or 0)
            if not bucket or bucket == "UNKNOWN" or n < LESSON_MIN_N:
                continue
            expectancy = float(b.get("expectancy") or 0.0)
            direction = _direction_of(expectancy)
            if direction is None:
                continue
            win_rate = float(b.get("win_rate") or 0.0)
            key = (dim, bucket)
            seen_keys.add(key)

            lesson = await db.hermes_lessons.find_one(
                {"user_id": user_id, "dimension": dim, "bucket": bucket}
            )
            if lesson is None:
                await db.hermes_lessons.insert_one(
                    new_lesson(user_id, dim, bucket, direction, n, expectancy, win_rate, date_str)
                )
                stats["created"] += 1
                continue
            # Idempotency: already scored this bucket today (EOD re-run) → skip.
            if lesson.get("last_scored_date") == date_str:
                stats["skipped_dup"] += 1
                continue
            # Terminal state — decayed lessons stop influencing anything.
            if lesson.get("status") == "decayed":
                continue

            prev_status = lesson.get("status")
            confirmed = (direction == lesson["direction"])
            patch = apply_observation(lesson, direction, n, expectancy, win_rate, date_str)
            await db.hermes_lessons.update_one(
                {"lesson_id": lesson["lesson_id"]}, {"$set": patch}
            )
            stats["confirmed" if confirmed else "contradicted"] += 1
            if patch["status"] == "active" and prev_status == "candidate":
                stats["promoted"] += 1
            if patch["status"] == "decayed" and prev_status != "decayed":
                stats["decayed"] += 1

    # Staleness decay for live lessons whose bucket did NOT trade today.
    try:
        live = await db.hermes_lessons.find(
            {"user_id": user_id, "status": {"$in": ["candidate", "active"]}}
        ).to_list(2000)
    except Exception as exc:
        logger.error("lesson staleness scan failed: %s", exc)
        live = []
    for lesson in live:
        if (lesson.get("dimension"), lesson.get("bucket")) in seen_keys:
            continue
        if is_stale(lesson, date_str):
            await db.hermes_lessons.update_one(
                {"lesson_id": lesson["lesson_id"]},
                {"$set": {"status": "decayed", "decay_reason": "stale", "decayed_at": date_str}},
            )
            stats["decayed"] += 1

    logger.info("hermes_lessons scored user=%s date=%s %s", user_id, date_str, stats)
    return stats


async def get_brain_health(db, user_id: str) -> Dict[str, Any]:
    """HSI-34 meta-metric — watch the brain get smarter (or not). Pure read."""
    lessons = await db.hermes_lessons.find({"user_id": user_id}, {"_id": 0}).to_list(5000)
    active = [l for l in lessons if l.get("status") == "active"]
    candidate = [l for l in lessons if l.get("status") == "candidate"]
    decayed = [l for l in lessons if l.get("status") == "decayed"]
    live = active + candidate
    tot_obs = sum(int(l.get("observations_count") or 0) for l in live)
    tot_correct = sum(int(l.get("correct_count") or 0) for l in live)
    top = sorted(active, key=lambda l: l.get("confidence") or 0, reverse=True)[:8]
    return {
        "active_lessons": len(active),
        "candidate_lessons": len(candidate),
        "decayed_lessons": len(decayed),
        "total_lessons": len(lessons),
        "avg_confidence_active": (
            round(sum(l.get("confidence") or 0 for l in active) / len(active), 3) if active else 0.0
        ),
        "overall_hit_rate": round(tot_correct / tot_obs, 3) if tot_obs else None,
        "total_observations": tot_obs,
        "top_active_lessons": [
            {
                "claim": l.get("claim"),
                "direction": l.get("direction"),
                "confidence": l.get("confidence"),
                "hit_rate": l.get("hit_rate"),
                "sample_size": l.get("sample_size"),
                "observations": l.get("observations_count"),
            }
            for l in top
        ],
        "as_of": _now_iso(),
    }
