"""RAE-4 — the regime router / capital allocator."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import regime_taxonomy as tax  # noqa: E402
from core.regime_router import route, enabled  # noqa: E402


def test_chop_and_event_stand_down():
    for reg in (tax.HIGH_VOL_CHOP, tax.EVENT):
        d = route(reg, 0.9, specialist="range_seller")
        assert d.stand_down and d.size_mult == 0.0


def test_seller_stands_down_off_regime():
    # a range seller on a trend day = the 2026-07-10 loss → stand down
    d = route(tax.TREND_UP, 0.95, specialist="range_seller")
    assert d.stand_down and d.size_mult == 0.0


def test_seller_active_on_range_and_inside():
    assert not route(tax.RANGE, 0.4, specialist="range_seller").stand_down
    # sellers co-own the quiet regime
    assert not route(tax.INSIDE_QUIET, 0.4, specialist="range_seller").stand_down


def test_trend_specialist_needs_high_confidence():
    lo = route(tax.TREND_UP, 0.70, specialist="trend_delta1_long")
    hi = route(tax.TREND_UP, 0.97, specialist="trend_delta1_long")
    assert lo.stand_down and lo.size_mult == 0.0
    assert not hi.stand_down and hi.size_mult > 0.0


def test_trend_size_scales_with_confidence():
    a = route(tax.TREND_UP, 0.91, specialist="trend_delta1_long").size_mult
    b = route(tax.TREND_UP, 0.99, specialist="trend_delta1_long").size_mult
    assert b > a  # more confident → bigger


def test_inside_specialist_owns_inside():
    d = route(tax.INSIDE_QUIET, 0.5, specialist="inside_mean_revert")
    assert not d.stand_down and d.size_mult > 0


def test_unknown_regime_defaults_to_range():
    d = route("SOMETHING_LIVE", 0.5, specialist="range_seller")
    assert d.regime == tax.RANGE and not d.stand_down


def test_generic_route_activates_owner():
    d = route(tax.RANGE, 0.5)
    assert d.active_specialists == ["range_seller"]


def test_enabled_defaults_off():
    os.environ.pop("RAE_ROUTER_ENABLED", None)
    assert enabled() is False
