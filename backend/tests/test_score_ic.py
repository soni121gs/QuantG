"""P5-M5 — information coefficient screen for the scoring systems."""
import os
import random
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.score_ic import information_coefficient  # noqa: E402


def test_predictive_score_detected():
    random.seed(1)
    # score linearly predicts pnl (plus noise) -> PREDICTIVE
    pairs = [(s := random.random(), s * 1000 + random.gauss(0, 50)) for _ in range(200)]
    r = information_coefficient("good", pairs)
    assert r.verdict == "PREDICTIVE" and r.ic > 0.3


def test_decoration_detected():
    random.seed(2)
    # score is pure noise vs pnl -> DECORATION
    pairs = [(random.random(), random.gauss(0, 500)) for _ in range(200)]
    r = information_coefficient("noise", pairs)
    assert r.verdict == "DECORATION"


def test_inverted_score_detected():
    random.seed(3)
    pairs = [(s := random.random(), -s * 1000 + random.gauss(0, 50)) for _ in range(200)]
    r = information_coefficient("backwards", pairs)
    assert r.verdict == "INVERTED" and r.ic < 0


def test_insufficient_data():
    r = information_coefficient("thin", [(1.0, 2.0)] * 5)
    assert r.verdict == "INSUFFICIENT_DATA" and r.ic is None


def test_ties_handled():
    # all identical scores -> zero variance -> insufficient/None, not a crash
    r = information_coefficient("flat", [(1.0, float(i)) for i in range(50)])
    assert r.verdict in ("DECORATION", "INSUFFICIENT_DATA")
