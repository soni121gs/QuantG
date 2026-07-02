import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.hermes_validator import evaluate_oos, hypothesis_from_lesson


def test_oos_passes_only_when_effect_clears_margin():
    result = evaluate_oos(
        [180, 120, 90, 130, 160],
        [-40, 10, -20, 0, 15],
        direction="good",
        min_trades=5,
        min_effect_inr=50,
    )

    assert result["status"] == "PASS"
    assert result["passed"] is True
    assert result["bucket_n"] == 5
    assert result["effect_inr"] >= 50


def test_oos_rejects_noise_even_with_positive_sign():
    result = evaluate_oos(
        [20, 5, 10, 15, 0],
        [0, 8, 6, 4, 7],
        direction="good",
        min_trades=5,
        min_effect_inr=50,
    )

    assert result["status"] == "FAIL"
    assert result["passed"] is False


def test_oos_blocks_small_samples():
    result = evaluate_oos(
        [250, 150],
        [-50, 20, 0, 10, -10],
        direction="good",
        min_trades=5,
        min_effect_inr=50,
    )

    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["passed"] is False


def test_bad_direction_can_pass_when_bucket_is_worse_than_control():
    result = evaluate_oos(
        [-180, -120, -90, -130, -160],
        [-20, 10, -15, 0, 5],
        direction="bad",
        min_trades=5,
        min_effect_inr=50,
    )

    assert result["status"] == "PASS"
    assert result["passed"] is True
    assert result["effect_inr"] >= 50


def test_hypothesis_from_lesson_uses_next_day_oos_window():
    lesson = {
        "lesson_id": "lsn_1",
        "user_id": "u1",
        "dimension": "structure",
        "bucket": "credit_spread",
        "direction": "good",
        "claim": "credit spreads good",
        "last_confirmed_at": "2026-07-03",
    }

    hyp = hypothesis_from_lesson(lesson)

    assert hyp["lesson_id"] == "lsn_1"
    assert hyp["oos_start"] == "2026-07-04"
    assert hyp["objective"] == "expectancy"
