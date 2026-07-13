"""HSI-51..54: Stage 5 gated advisor.

OOS-validated lessons (Stage 4) become (a) approval-gated config proposals and
(b) read-only advisory context the strategy gates MAY consult. This module is the
deterministic core — it NEVER trades, NEVER edits code, and every behaviour-changing
path is either human-approved (a `draft_config_change` pending action) or behind a
flag that defaults to observe-only.

The five guarantees:
  - only lessons with `oos_passed=true` can drive a proposal (judge-first, Stage 4);
  - only DB-durable fields can be auto-applied (`required_capital`,
    `options.structure`) — everything else surfaces an "edit-in-template" task
    because a DB-only edit is silently re-synced away (CLAUDE.md KEY MECHANIC);
  - every applied change stores its prior value (reversible — HSI-54);
  - changes are rate-limited (≤N/week — HSI-54);
  - a change whose post-change expectancy regresses is auto-reverted (HSI-54).

Pure helpers (advice/rate-limit/regression) are unit-tested; the async DB wrappers
are thin.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("quantg.hermes_advisor")

# Observe-only by default — the advisory multiplier does NOT change live behaviour
# until the founder flips this on.
HERMES_ADVICE_ENABLED = os.environ.get("HERMES_ADVICE_ENABLED", "false").lower() == "true"
HERMES_MAX_CHANGES_PER_WEEK = int(os.environ.get("HERMES_MAX_CHANGES_PER_WEEK", "2"))
HERMES_AUTOREVERT_MIN_TRADES = int(os.environ.get("HERMES_AUTOREVERT_MIN_TRADES", "10"))
HERMES_ADVICE_MAX_TILT = float(os.environ.get("HERMES_ADVICE_MAX_TILT", "0.4"))

# Fields that survive the startup template re-sync (CLAUDE.md KEY MECHANIC). Any
# other field applied DB-only is rewritten on the next backend restart, so we refuse
# to "apply" it and instead surface an edit-in-template task.
DB_DURABLE_FIELDS = ("required_capital", "visual_config.options.structure")

# Founder-directed override lane: fields the OWNER may change via the Ask Agent
# WITHOUT an OOS lesson (diagnostic / risk knobs). The human owner IS the gate, so
# these skip the judge-first lesson requirement — but stay reversible, rate-limited,
# audit-logged, paper-only, and confined to this whitelist so the agent can never
# inject an arbitrary field. DB-only strategy rows (all RAE/custom seeds) persist
# these directly; a template-backed strategy re-syncs any non-DB_DURABLE_FIELDS
# field on restart, which the approve path flags.
FOUNDER_EDITABLE_FIELDS = (
    "required_capital",
    "visual_config.options.required_capital",
    "visual_config.options.credit_tp_frac",
    "visual_config.options.credit_sl_mult",
    "visual_config.risk.cooldown_minutes",
    "visual_config.risk.max_trades_day",
    "visual_config.risk.daily_loss_limit",
    "visual_config.risk.time_exit_minutes",
    "visual_config.risk.stop_loss_pct",
    "visual_config.risk.take_profit_pct",
    "max_trades_day",
)


def is_founder_editable_field(field: str) -> bool:
    return field in FOUNDER_EDITABLE_FIELDS or field in DB_DURABLE_FIELDS


def coerce_config_value(field: str, value):
    """Light type-coercion so a JSON string/number lands as the right scalar.
    Count-like fields → int; everything else editable → float; fall back to raw."""
    _int_fields = ("cooldown_minutes", "max_trades_day", "time_exit_minutes")
    try:
        leaf = field.split(".")[-1]
        if leaf in _int_fields:
            return int(float(value))
        if leaf == "structure":
            return str(value)
        return float(value)
    except (TypeError, ValueError):
        return value

# lesson dimension -> the attribution field a strategy carries, for advice lookup.
LESSON_DIMENSION_FIELD = {
    "structure": "structure",
    "regime": "regime_at_entry",
    "exposure_bias": "exposure_bias",
    "exit_reason": "exit_reason",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- pure: advice compilation ----------------------------------------------
def advice_from_lessons(lessons: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn active, OOS-passed lessons into a confidence-multiplier surface.

    A 'good' lesson tilts its bucket up (>1), a 'bad' one down (<1), scaled by the
    lesson's confidence and capped at ±HERMES_ADVICE_MAX_TILT. Only active +
    oos_passed lessons contribute — everything else is ignored.
    """
    multipliers: Dict[str, Dict[str, float]] = {}
    sources: List[Dict[str, Any]] = []
    for lsn in lessons:
        if lsn.get("status") != "active" or not lsn.get("oos_passed"):
            continue
        dim = lsn.get("dimension")
        bucket = lsn.get("bucket")
        if dim not in LESSON_DIMENSION_FIELD or bucket is None:
            continue
        conf = max(0.0, min(1.0, float(lsn.get("confidence") or 0.0)))
        tilt = HERMES_ADVICE_MAX_TILT * conf
        mult = 1.0 + tilt if lsn.get("direction") == "good" else 1.0 - tilt
        multipliers.setdefault(dim, {})[str(bucket)] = round(mult, 4)
        sources.append({"lesson_id": lsn.get("lesson_id"), "dimension": dim,
                        "bucket": bucket, "direction": lsn.get("direction"),
                        "confidence": round(conf, 3)})
    return {"multipliers": multipliers, "sources": sources, "generated_at": _now_iso()}


def confidence_multiplier(advice: Dict[str, Any], dimensions: Dict[str, Any]) -> float:
    """Combined multiplier for a candidate trade's dimensions (product, defaults 1.0)."""
    if not advice:
        return 1.0
    mults = advice.get("multipliers") or {}
    out = 1.0
    for dim, bucket in dimensions.items():
        val = mults.get(dim, {}).get(str(bucket))
        if val:
            out *= float(val)
    return round(out, 4)


# ---- pure: safety rails -----------------------------------------------------
def within_rate_limit(recent_change_count: int, max_per_week: int = HERMES_MAX_CHANGES_PER_WEEK) -> bool:
    return recent_change_count < max_per_week


def expectancy_regressed(pre_expectancy: float, post_expectancy: float, post_n: int,
                         min_trades: int = HERMES_AUTOREVERT_MIN_TRADES) -> bool:
    """A change is a regression once it has a real post-window sample that is both
    negative AND worse than the pre-change expectancy."""
    if post_n < min_trades:
        return False
    return post_expectancy < 0 and post_expectancy < pre_expectancy


def is_db_durable_field(field: str) -> bool:
    return field in DB_DURABLE_FIELDS


# ---- async: DB wrappers -----------------------------------------------------
async def compile_hermes_advice(db, user_id: str) -> Dict[str, Any]:
    """Rebuild db.hermes_advice(_id=user_id) from active OOS-passed lessons."""
    lessons = await db.hermes_lessons.find(
        {"user_id": user_id, "status": "active", "oos_passed": True}).to_list(200)
    advice = advice_from_lessons(lessons)
    advice.update({"_id": user_id, "user_id": user_id, "enabled": HERMES_ADVICE_ENABLED,
                   "active_lessons": len(advice["sources"])})
    await db.hermes_advice.replace_one({"_id": user_id}, advice, upsert=True)
    return advice


async def get_hermes_advice(db, user_id: str) -> Optional[Dict[str, Any]]:
    """Read the cached advice. Strategy gates may consult it — observe-only unless
    HERMES_ADVICE_ENABLED. Returns None when no advice / disabled."""
    if not HERMES_ADVICE_ENABLED:
        return None
    return await db.hermes_advice.find_one({"_id": user_id})


async def count_recent_changes(db, user_id: str, days: int = 7) -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return await db.hermes_applied_changes.count_documents(
        {"user_id": user_id, "applied_at": {"$gte": since}, "status": "applied"})


async def record_applied_change(db, user_id: str, change: Dict[str, Any]) -> str:
    change_id = "chg_" + uuid.uuid4().hex[:16]
    doc = {"change_id": change_id, "user_id": user_id, "status": "applied",
           "applied_at": _now_iso(), **change}
    await db.hermes_applied_changes.insert_one(doc)
    return change_id


async def active_change_lesson_by_strategy(db, user_id: str) -> Dict[str, str]:
    """{strategy_id: lesson_id} for changes still in effect — HSI-53 attribution tag."""
    out: Dict[str, str] = {}
    async for c in db.hermes_applied_changes.find({"user_id": user_id, "status": "applied"}):
        sid = c.get("strategy_id")
        if sid and c.get("lesson_id"):
            out[sid] = c["lesson_id"]
    return out


async def check_and_autorevert(db, user_id: str, rollup_fn) -> List[Dict[str, Any]]:
    """Auto-revert applied changes whose post-change expectancy regressed (HSI-54).

    `rollup_fn(strategy_id, since)` -> {"n", "expectancy"} for the post-change window,
    injected so this stays testable without the attribution engine.
    """
    reverted: List[Dict[str, Any]] = []
    async for c in db.hermes_applied_changes.find({"user_id": user_id, "status": "applied"}):
        post = await rollup_fn(c.get("strategy_id"), c.get("applied_at")) or {}
        pre_exp = float(c.get("pre_expectancy") or 0.0)
        if not expectancy_regressed(pre_exp, float(post.get("expectancy") or 0.0), int(post.get("n") or 0)):
            continue
        # revert the field to its stored prior value
        if c.get("strategy_id") and c.get("field") and "prior_value" in c:
            await db.strategies.update_one(
                {"id": c["strategy_id"], "user_id": user_id},
                {"$set": {c["field"]: c["prior_value"], "updated_at": _now_iso()}})
        await db.hermes_applied_changes.update_one(
            {"change_id": c["change_id"]},
            {"$set": {"status": "reverted", "reverted_at": _now_iso(),
                      "revert_reason": "post-change expectancy regressed",
                      "post_expectancy": round(float(post.get("expectancy") or 0.0), 2),
                      "post_n": int(post.get("n") or 0)}})
        reverted.append({"change_id": c["change_id"], "strategy_id": c.get("strategy_id"),
                         "field": c.get("field")})
    return reverted
