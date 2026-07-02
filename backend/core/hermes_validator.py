"""HSI Stage 4 - deterministic out-of-sample validator.

Hermes lessons are allowed to become trading advice only after this judge tests
them on held-out trade_attribution rows. This module never places orders and
never changes live strategy config.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

OOS_MIN_TRADES = int(os.environ.get("HERMES_OOS_MIN_TRADES", "12"))
OOS_MIN_EFFECT_INR = float(os.environ.get("HERMES_OOS_MIN_EFFECT_INR", "50"))
OOS_MAX_TESTS_PER_RUN = int(os.environ.get("HERMES_OOS_MAX_TESTS_PER_RUN", "25"))

DIMENSION_FIELDS = {
    "strategy": "strategy_name",
    "structure": "structure",
    "regime": "regime_at_entry",
    "exit_reason": "exit_reason",
    "exposure_bias": "exposure_bias",
    "hold_bucket": "hold_bucket",
    "time_of_day": "time_of_day",
    "underlying": "underlying",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_day(date_str: str) -> str:
    try:
        return (datetime.strptime(str(date_str)[:10], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        return str(date_str or "")[:10]


def _mean(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def evaluate_oos(
    bucket_pnls: List[float],
    control_pnls: List[float],
    *,
    direction: str,
    min_trades: int = OOS_MIN_TRADES,
    min_effect_inr: float = OOS_MIN_EFFECT_INR,
) -> Dict[str, Any]:
    bucket_n = len(bucket_pnls)
    control_n = len(control_pnls)
    bucket_expectancy = _mean(bucket_pnls)
    control_expectancy = _mean(control_pnls)
    raw_delta = bucket_expectancy - control_expectancy
    signed_effect = raw_delta if direction == "good" else -raw_delta
    enough_sample = bucket_n >= min_trades and control_n >= min_trades
    direction_ok = bucket_expectancy > 0 if direction == "good" else bucket_expectancy < 0
    passed = enough_sample and direction_ok and signed_effect >= min_effect_inr
    if not enough_sample:
        status = "INSUFFICIENT_DATA"
        reason = f"OOS sample too small: bucket n={bucket_n}, control n={control_n}, need >= {min_trades}"
    elif passed:
        status = "PASS"
        reason = f"OOS effect INR {signed_effect:.2f}/trade clears margin {min_effect_inr:.2f}"
    else:
        status = "FAIL"
        reason = f"OOS effect INR {signed_effect:.2f}/trade does not clear margin {min_effect_inr:.2f}"
    return {
        "status": status,
        "passed": passed,
        "reason": reason,
        "bucket_n": bucket_n,
        "control_n": control_n,
        "bucket_expectancy": round(bucket_expectancy, 2),
        "control_expectancy": round(control_expectancy, 2),
        "effect_inr": round(signed_effect, 2),
        "raw_delta_inr": round(raw_delta, 2),
        "min_trades": min_trades,
        "min_effect_inr": min_effect_inr,
    }


def hypothesis_from_lesson(lesson: Dict[str, Any]) -> Dict[str, Any]:
    source_end = (
        str(lesson.get("last_confirmed_at") or "")[:10]
        or str(lesson.get("last_scored_date") or "")[:10]
        or str(lesson.get("created_at") or "")[:10]
    )
    return {
        "hypothesis_id": f"hyp_{lesson.get('lesson_id') or uuid.uuid4().hex[:12]}",
        "lesson_id": lesson.get("lesson_id"),
        "user_id": lesson.get("user_id"),
        "dimension": lesson.get("dimension"),
        "bucket": lesson.get("bucket"),
        "direction": lesson.get("direction"),
        "claim": lesson.get("claim"),
        "objective": "expectancy",
        "source_window_end": source_end,
        "oos_start": _next_day(source_end) if source_end else None,
    }


async def _ensure_indexes(db) -> None:
    await db.hermes_hypothesis_tests.create_index("test_id", unique=True)
    await db.hermes_hypothesis_tests.create_index([("user_id", 1), ("lesson_id", 1), ("tested_at", -1)])
    await db.hermes_hypothesis_tests.create_index([("user_id", 1), ("status", 1), ("tested_at", -1)])


async def validate_hypothesis(db, hypothesis: Dict[str, Any]) -> Dict[str, Any]:
    await _ensure_indexes(db)
    user_id = hypothesis.get("user_id")
    dimension = str(hypothesis.get("dimension") or "")
    bucket = hypothesis.get("bucket")
    direction = str(hypothesis.get("direction") or "")
    field = DIMENSION_FIELDS.get(dimension)
    oos_start = hypothesis.get("oos_start")
    if not user_id or not field or not bucket or direction not in {"good", "bad"} or not oos_start:
        result = {
            "status": "INVALID",
            "passed": False,
            "reason": "Hypothesis missing user_id, supported dimension, bucket, direction, or OOS start",
            "bucket_n": 0,
            "control_n": 0,
        }
    else:
        rows = await db.trade_attribution.find(
            {"user_id": user_id, "date_ist": {"$gte": str(oos_start)[:10]}},
            {"_id": 0, field: 1, "realized_pnl": 1},
        ).to_list(200_000)
        bucket_pnls = [float(r.get("realized_pnl") or 0.0) for r in rows if r.get(field) == bucket]
        control_pnls = [float(r.get("realized_pnl") or 0.0) for r in rows if r.get(field) != bucket]
        result = evaluate_oos(bucket_pnls, control_pnls, direction=direction)

    test_doc = {
        "test_id": "oos_" + uuid.uuid4().hex[:16],
        "tested_at": _now_iso(),
        "user_id": user_id,
        "hypothesis": hypothesis,
        "lesson_id": hypothesis.get("lesson_id"),
        "dimension": dimension,
        "bucket": bucket,
        "direction": direction,
        **result,
    }
    await db.hermes_hypothesis_tests.insert_one(test_doc)
    if hypothesis.get("lesson_id"):
        patch = {
            "oos_status": result["status"],
            "oos_passed": bool(result.get("passed")),
            "last_oos_tested_at": test_doc["tested_at"],
            "last_oos_result": {k: v for k, v in result.items() if k != "passed"},
        }
        if result.get("passed"):
            patch["oos_validated_at"] = test_doc["tested_at"]
        await db.hermes_lessons.update_one({"lesson_id": hypothesis["lesson_id"]}, {"$set": patch})
    test_doc.pop("_id", None)
    return test_doc


async def validate_lessons(db, user_id: str, *, limit: int = OOS_MAX_TESTS_PER_RUN) -> Dict[str, Any]:
    lessons = await db.hermes_lessons.find(
        {"user_id": user_id, "status": {"$in": ["candidate", "active"]}},
        {"_id": 0},
    ).sort("confidence", -1).limit(max(1, min(limit, OOS_MAX_TESTS_PER_RUN))).to_list(limit)
    results = []
    for lesson in lessons:
        results.append(await validate_hypothesis(db, hypothesis_from_lesson(lesson)))
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "tested": len(results),
        "counts": counts,
        "results": results,
        "as_of": _now_iso(),
        "judge_first": True,
    }


async def get_validation_summary(db, user_id: str) -> Dict[str, Any]:
    latest = await db.hermes_hypothesis_tests.find(
        {"user_id": user_id},
        {"_id": 0},
    ).sort("tested_at", -1).limit(50).to_list(50)
    counts: Dict[str, int] = {}
    for row in latest:
        counts[row.get("status", "UNKNOWN")] = counts.get(row.get("status", "UNKNOWN"), 0) + 1
    return {
        "recent_tests": latest,
        "counts": counts,
        "pass_count": counts.get("PASS", 0),
        "fail_count": counts.get("FAIL", 0),
        "insufficient_count": counts.get("INSUFFICIENT_DATA", 0),
        "judge_first": True,
        "message": "No lesson can influence trading until an OOS test passes and the founder approves the downstream action.",
        "as_of": _now_iso(),
    }
