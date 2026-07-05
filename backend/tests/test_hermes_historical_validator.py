"""HSI Stage-4 extension — validate structure lessons vs bhavcopy OOS (pure + async)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest  # noqa: E402

from core import hermes_historical_validator as hv  # noqa: E402


# ---- pure verdict -----------------------------------------------------------
def test_good_lesson_confirmed_by_positive_edge():
    v = hv.historical_verdict(expectancy=214, n=106, direction="good")
    assert v["status"] == hv.PASS and v["passed"] is True
    assert v["direction_actual"] == "good"


def test_bad_lesson_confirmed_by_negative_edge():
    v = hv.historical_verdict(expectancy=-90, n=60, direction="bad")
    assert v["status"] == hv.PASS and v["passed"] is True


def test_direction_mismatch_fails():
    # lesson says good but the data is negative -> not confirmed
    v = hv.historical_verdict(expectancy=-90, n=60, direction="good")
    assert v["status"] == hv.FAIL and v["passed"] is False


def test_thin_sample_insufficient():
    v = hv.historical_verdict(expectancy=214, n=5, direction="good")
    assert v["status"] == hv.INSUFFICIENT and v["passed"] is False


def test_tiny_effect_fails_even_if_sign_matches():
    v = hv.historical_verdict(expectancy=4, n=100, direction="good", min_effect=50)
    assert v["passed"] is False


def test_canonical_strategy_shapes():
    s = hv.canonical_strategy("credit_spread")
    assert s["visual_config"]["options"]["structure"] == "credit_spread"
    assert s["_params"]["exit_mode"] == "expiry"
    assert hv.canonical_strategy("nonsense") is None


# ---- async wrapper with mocked backtester + fake DB -------------------------
class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    async def to_list(self, n):
        return list(self._rows)


class _Lessons:
    def __init__(self, rows):
        self.rows = rows
        self.updates = {}

    def find(self, q):
        return _Cursor([r for r in self.rows if r.get("dimension") == q.get("dimension")])

    async def update_one(self, q, upd):
        self.updates[q["lesson_id"]] = upd["$set"]


class _DB:
    def __init__(self, lessons):
        self.hermes_lessons = lessons


@pytest.mark.asyncio
async def test_validate_structure_lessons_stamps_oos():
    lessons = _Lessons([
        {"lesson_id": "l1", "user_id": "u", "dimension": "structure", "bucket": "credit_spread", "direction": "good"},
        {"lesson_id": "l2", "user_id": "u", "dimension": "structure", "bucket": "single_leg", "direction": "bad"},
        {"lesson_id": "l3", "user_id": "u", "dimension": "regime", "bucket": "RANGE", "direction": "good"},  # not structure
    ])
    db = _DB(lessons)

    def fake_bt(structure):
        return {"credit_spread": {"verdict": "CANDIDATE_EDGE", "overall": {"n": 106, "expectancy": 214}},
                "single_leg": {"verdict": "NO_EDGE_NEGATIVE", "overall": {"n": 200, "expectancy": -85}}}[structure]

    out = await hv.validate_structure_lessons(db, "u", backtest_fn=fake_bt)
    assert out["validated"] == 2
    assert lessons.updates["l1"]["oos_passed"] is True
    assert lessons.updates["l2"]["oos_passed"] is True   # bad lesson, negative edge -> confirmed
    assert "l3" not in lessons.updates                    # regime lesson untouched
    assert lessons.updates["l1"]["oos_source"] == "bhavcopy_2yr"


@pytest.mark.asyncio
async def test_backtest_runs_once_per_structure():
    lessons = _Lessons([
        {"lesson_id": "a", "user_id": "u", "dimension": "structure", "bucket": "credit_spread", "direction": "good"},
        {"lesson_id": "b", "user_id": "u", "dimension": "structure", "bucket": "credit_spread", "direction": "bad"},
    ])
    calls = {"n": 0}

    def fake_bt(structure):
        calls["n"] += 1
        return {"overall": {"n": 106, "expectancy": 214}}

    await hv.validate_structure_lessons(_DB(lessons), "u", backtest_fn=fake_bt)
    assert calls["n"] == 1  # memoized per structure
    assert lessons.updates["a"]["oos_passed"] is True    # good confirmed
    assert lessons.updates["b"]["oos_passed"] is False   # bad contradicted by positive edge
