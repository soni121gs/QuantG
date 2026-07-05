"""HSI-51..54 — Stage 5 gated advisor (pure logic + async wrappers with a fake DB)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from core import hermes_advisor as ha  # noqa: E402


def _lesson(dim, bucket, direction, conf, status="active", oos=True):
    return {"lesson_id": f"lsn_{bucket}", "dimension": dim, "bucket": bucket,
            "direction": direction, "confidence": conf, "status": status, "oos_passed": oos}


# ---- HSI-52: advice compilation (pure) --------------------------------------
def test_advice_only_uses_active_oos_passed_lessons():
    lessons = [
        _lesson("structure", "credit_spread", "good", 1.0),
        _lesson("structure", "debit_spread", "bad", 1.0),
        _lesson("regime", "RANGE", "good", 0.5, status="candidate"),   # not active -> ignored
        _lesson("regime", "TREND_UP", "bad", 1.0, oos=False),          # not oos -> ignored
    ]
    advice = ha.advice_from_lessons(lessons)
    mults = advice["multipliers"]
    assert mults["structure"]["credit_spread"] == round(1 + ha.HERMES_ADVICE_MAX_TILT, 4)
    assert mults["structure"]["debit_spread"] == round(1 - ha.HERMES_ADVICE_MAX_TILT, 4)
    assert "regime" not in mults
    assert len(advice["sources"]) == 2


def test_confidence_multiplier_products():
    advice = ha.advice_from_lessons([
        _lesson("structure", "credit_spread", "good", 1.0),
        _lesson("regime", "RANGE", "good", 0.5),
    ])
    m = ha.confidence_multiplier(advice, {"structure": "credit_spread", "regime": "RANGE"})
    exp = round((1 + ha.HERMES_ADVICE_MAX_TILT) * (1 + ha.HERMES_ADVICE_MAX_TILT * 0.5), 4)
    assert m == exp
    # unknown bucket -> no tilt
    assert ha.confidence_multiplier(advice, {"structure": "single_leg"}) == 1.0
    assert ha.confidence_multiplier({}, {"structure": "credit_spread"}) == 1.0


# ---- HSI-54: safety rails (pure) --------------------------------------------
def test_rate_limit():
    assert ha.within_rate_limit(0, 2) is True
    assert ha.within_rate_limit(1, 2) is True
    assert ha.within_rate_limit(2, 2) is False


def test_expectancy_regression_needs_sample_and_negative_and_worse():
    assert ha.expectancy_regressed(pre_expectancy=100, post_expectancy=-50, post_n=20) is True
    assert ha.expectancy_regressed(100, -50, post_n=3) is False        # thin sample
    assert ha.expectancy_regressed(-200, -50, post_n=20) is False      # negative but improved
    assert ha.expectancy_regressed(100, 40, post_n=20) is False        # still positive


def test_db_durable_field_gate():
    assert ha.is_db_durable_field("required_capital") is True
    assert ha.is_db_durable_field("visual_config.options.structure") is True
    assert ha.is_db_durable_field("risk.daily_loss_limit") is False


# ---- async wrappers with a minimal fake collection --------------------------
class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, n):
        return list(self._rows)

    def __aiter__(self):
        async def gen():
            for r in self._rows:
                yield r
        return gen()


class _FakeColl:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.updates = []

    def find(self, q=None):
        return _FakeCursor(list(self.rows))

    async def count_documents(self, q):
        return len(self.rows)

    async def insert_one(self, doc):
        self.rows.append(doc)

    async def replace_one(self, q, doc, upsert=False):
        self.rows = [doc]

    async def find_one(self, q):
        return self.rows[0] if self.rows else None

    async def update_one(self, q, upd):
        self.updates.append((q, upd))
        for r in self.rows:
            if r.get("change_id") == q.get("change_id"):
                r.update(upd.get("$set", {}))


class _FakeDB:
    def __init__(self, **colls):
        for k, v in colls.items():
            setattr(self, k, v)


@pytest.mark.asyncio
async def test_compile_hermes_advice_persists():
    lessons = _FakeColl([_lesson("structure", "credit_spread", "good", 1.0)])
    advice_coll = _FakeColl([])
    db = _FakeDB(hermes_lessons=lessons, hermes_advice=advice_coll)
    advice = await ha.compile_hermes_advice(db, "u1")
    assert advice["active_lessons"] == 1
    assert advice_coll.rows[0]["_id"] == "u1"


@pytest.mark.asyncio
async def test_autorevert_reverts_regressed_change():
    changes = _FakeColl([{
        "change_id": "chg_1", "user_id": "u1", "status": "applied",
        "strategy_id": "s1", "field": "required_capital", "prior_value": 8000.0,
        "proposed_value": 16000.0, "pre_expectancy": 120.0, "applied_at": "2026-07-01T00:00:00+00:00",
        "lesson_id": "lsn_x",
    }])
    strategies = _FakeColl([{"id": "s1", "required_capital": 16000.0}])
    db = _FakeDB(hermes_applied_changes=changes, strategies=strategies)

    async def rollup(sid, since):
        return {"n": 20, "expectancy": -80.0}   # regressed

    reverted = await ha.check_and_autorevert(db, "u1", rollup)
    assert len(reverted) == 1
    assert changes.rows[0]["status"] == "reverted"


@pytest.mark.asyncio
async def test_autorevert_keeps_healthy_change():
    changes = _FakeColl([{
        "change_id": "chg_2", "user_id": "u1", "status": "applied",
        "strategy_id": "s1", "field": "required_capital", "prior_value": 8000.0,
        "pre_expectancy": 120.0, "applied_at": "2026-07-01T00:00:00+00:00",
    }])
    db = _FakeDB(hermes_applied_changes=changes, strategies=_FakeColl([]))

    async def rollup(sid, since):
        return {"n": 20, "expectancy": 200.0}   # improved

    reverted = await ha.check_and_autorevert(db, "u1", rollup)
    assert reverted == []
    assert changes.rows[0]["status"] == "applied"
