"""RAE-5 — regime-aware exit trail geometry."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import regime_taxonomy as tax  # noqa: E402
from core.dynamic_exit import TRAIL_ARM_FRAC, TRAIL_GIVEBACK_FRAC  # noqa: E402
from core.regime_exit import regime_exit_params  # noqa: E402


def test_disabled_returns_global_defaults_noop():
    p = regime_exit_params(tax.RANGE, enabled=False)
    assert p["arm_frac"] == TRAIL_ARM_FRAC and p["giveback_frac"] == TRAIL_GIVEBACK_FRAC


def test_trend_trails_wider_than_range_when_enabled():
    trend = regime_exit_params(tax.TREND_UP, enabled=True)
    rng = regime_exit_params(tax.RANGE, enabled=True)
    # trend gives back MORE before locking (lets winners run); range books faster
    assert trend["giveback_frac"] > rng["giveback_frac"]


def test_inside_is_tightest():
    inside = regime_exit_params(tax.INSIDE_QUIET, enabled=True)
    rng = regime_exit_params(tax.RANGE, enabled=True)
    assert inside["giveback_frac"] <= rng["giveback_frac"]


def test_unknown_regime_falls_back_to_default():
    p = regime_exit_params("WHATEVER", enabled=True)
    assert p["arm_frac"] == TRAIL_ARM_FRAC and p["giveback_frac"] == TRAIL_GIVEBACK_FRAC
