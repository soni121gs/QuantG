"""HSI Stage 3 — scored lesson store tests (pure + integration on a fake DB)."""
import os
import sys
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import hermes_lessons as HL
from core.hermes_lessons import (
    new_lesson, apply_observation, is_stale, _confidence, _direction_of, _days_between,
    score_and_update_lessons, get_brain_health, _promotion_stats,
)


# ── Pure functions ───────────────────────────────────────────────────────────────

def test_confidence_rewards_accuracy_and_sample():
    assert _confidence(1, 1) < _confidence(6, 6)          # more sample → more confidence
    assert _confidence(6, 6) == 1.0                        # perfect + full sample
    assert _confidence(3, 6) < _confidence(6, 6)           # lower accuracy → lower
    assert _confidence(0, 0) == 0.0


def test_direction_of():
    assert _direction_of(120.0) == "good"
    assert _direction_of(-50.0) == "bad"
    assert _direction_of(0.0) is None


def test_days_between():
    assert _days_between("2026-07-01", "2026-07-11") == 10
    assert _days_between("bad", "2026-07-11") == 0


def test_new_lesson_is_low_confidence_candidate():
    l = new_lesson("u1", "structure", "credit_spread", "good", 5, 120.0, 0.67, "2026-07-01")
    assert l["status"] == "candidate"
    assert l["direction"] == "good"
    assert l["correct_count"] == 1 and l["observations_count"] == 1
    assert 0.0 < l["confidence"] < 0.3          # brand-new → low confidence
    assert "credit_spread" in l["claim"]


def test_apply_observation_does_not_promote_noise_after_three_confirmations():
    l = new_lesson("u1", "structure", "credit_spread", "good", 5, 120.0, 0.67, "2026-07-01")
    # Two more same-direction confirmations (K=3 total) → active.
    for i, d in enumerate(("2026-07-02", "2026-07-03")):
        patch_fields = apply_observation(l, "good", 5, 100.0, 0.6, d)
        l.update(patch_fields)
    assert l["correct_count"] == 3
    assert l["status"] == "candidate"
    assert l["hit_rate"] == 1.0
    assert l["promotion_test"]["passes_multiple_testing"] is False


def test_promotion_stats_passes_only_after_significant_evidence():
    assert _promotion_stats(3, 3)["passes_multiple_testing"] is False
    assert _promotion_stats(10, 10)["passes_multiple_testing"] is True


def test_apply_observation_contradiction_decays_on_low_hitrate():
    l = new_lesson("u1", "regime", "RANGE", "good", 5, 80.0, 0.6, "2026-07-01")
    # Feed contradictions until obs>=DECAY_MIN_OBS and hit_rate<0.5.
    l.update(apply_observation(l, "bad", 5, -30.0, 0.2, "2026-07-02"))  # 1/2
    l.update(apply_observation(l, "bad", 5, -30.0, 0.2, "2026-07-03"))  # 1/3
    l.update(apply_observation(l, "bad", 5, -30.0, 0.2, "2026-07-04"))  # 1/4 → hit_rate .25, obs 4
    assert l["status"] == "decayed"
    assert l["decay_reason"] == "low_hit_rate"


def test_is_stale():
    l = new_lesson("u1", "structure", "debit_spread", "bad", 5, -90.0, 0.2, "2026-07-01")
    assert is_stale(l, "2026-07-05") is False           # 4 days — fresh
    assert is_stale(l, "2026-07-20") is True            # 19 days — stale


# ── Fake async DB for integration ────────────────────────────────────────────────

class _Cursor:
    def __init__(self, docs): self._docs = docs
    async def to_list(self, n=None): return [dict(d) for d in self._docs]


class _Collection:
    def __init__(self): self.docs = []
    async def create_index(self, *a, **k): return None
    def _match(self, d, query):
        for k, v in query.items():
            if isinstance(v, dict):
                if "$in" in v and d.get(k) not in v["$in"]: return False
                if "$ne" in v and d.get(k) == v["$ne"]: return False
            elif d.get(k) != v:
                return False
        return True
    async def find_one(self, query, projection=None):
        for d in self.docs:
            if self._match(d, query): return dict(d)
        return None
    async def insert_one(self, doc): self.docs.append(dict(doc))
    async def update_one(self, query, update):
        for d in self.docs:
            if self._match(d, query):
                d.update(update.get("$set", {})); return
    def find(self, query=None, projection=None):
        return _Cursor([d for d in self.docs if self._match(d, query or {})])


class _DB:
    def __init__(self): self.hermes_lessons = _Collection()


def _rollup(dim_map):
    """Return an async attribution_rollup stub driven by {dim: [buckets]}."""
    async def _fn(db, user_id, since, group_by):
        return dim_map.get(group_by, [])
    return _fn


def _bucket(name, n, expectancy, win_rate=0.6):
    return {"bucket": name, "n": n, "expectancy": expectancy, "win_rate": win_rate,
            "net_pnl": expectancy * n, "wins": int(win_rate * n), "losses": n - int(win_rate * n)}


@pytest.mark.asyncio
async def test_score_creates_then_promotes_and_is_idempotent():
    db = _DB()
    day_map = {"structure": [_bucket("credit_spread", 5, 150.0)]}
    with patch("core.trade_attribution.attribution_rollup", _rollup(day_map)):
        # Day 1 — creates a candidate.
        s1 = await score_and_update_lessons(db, "u1", "2026-07-01")
        assert s1["created"] == 1
        assert db.hermes_lessons.docs[0]["status"] == "candidate"

        # Idempotent: re-run same day does not double-count.
        s1b = await score_and_update_lessons(db, "u1", "2026-07-01")
        assert s1b["skipped_dup"] == 1 and s1b["created"] == 0
        assert db.hermes_lessons.docs[0]["observations_count"] == 1

        # Days 2 & 3 — confirmations promote candidate → active.
        await score_and_update_lessons(db, "u1", "2026-07-02")
        s3 = await score_and_update_lessons(db, "u1", "2026-07-03")
        lesson = db.hermes_lessons.docs[0]
        assert lesson["status"] == "candidate"
        assert lesson["correct_count"] == 3
        assert s3["promoted"] == 0


@pytest.mark.asyncio
async def test_brain_health_summary():
    db = _DB()
    day_map = {"structure": [_bucket("credit_spread", 5, 150.0)],
               "regime": [_bucket("RANGE", 6, 90.0)]}
    with patch("core.trade_attribution.attribution_rollup", _rollup(day_map)):
        await score_and_update_lessons(db, "u1", "2026-07-01")
    health = await get_brain_health(db, "u1")
    assert health["total_lessons"] == 2
    assert health["candidate_lessons"] == 2
    assert health["active_lessons"] == 0
    assert health["overall_hit_rate"] == 1.0
    assert len(health["top_active_lessons"]) == 0   # none active yet
