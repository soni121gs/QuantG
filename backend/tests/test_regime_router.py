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


def test_chop_veto_off_lets_seller_trade(monkeypatch):
    # founder-directed 2026-07-16: RAE_CHOP_STANDDOWN=false routes a HIGH_VOL_CHOP
    # day as RANGE so the range seller trades instead of standing down. EVENT still
    # stands down (macro fat-tail is not overridden).
    monkeypatch.setenv("RAE_CHOP_STANDDOWN", "false")
    seller = route(tax.HIGH_VOL_CHOP, 0.9, specialist="range_seller")
    assert not seller.stand_down and seller.size_mult > 0.0 and seller.regime == tax.RANGE
    event = route(tax.EVENT, 0.9, specialist="range_seller")
    assert event.stand_down and event.size_mult == 0.0
    # a trend specialist on chop-routed-as-range still stands down (does not own RANGE)
    trend = route(tax.HIGH_VOL_CHOP, 0.95, specialist="trend_delta1")
    assert trend.stand_down


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


# --- 2026-07-22: the confidence leak that let sellers trade a TREND_DOWN day ---

def test_low_confidence_fine_read_defers_to_coarse_regime():
    """The fine classifier's fall-through label is RANGE — the sellers' home — so an
    immature 'don't know yet' used to read as a full-size green light. Below
    FINE_MIN_CONF we defer to the mature coarse regime instead."""
    d = route(tax.RANGE, 0.40, specialist="range_seller", fallback_regime=tax.TREND_DOWN)
    assert d.regime == tax.TREND_DOWN
    assert d.stand_down and d.size_mult == 0.0
    assert any("deferring to coarse" in r for r in d.reasons)


def test_confident_fine_read_ignores_the_coarse_fallback():
    d = route(tax.RANGE, 0.80, specialist="range_seller", fallback_regime=tax.TREND_DOWN)
    assert d.regime == tax.RANGE and not d.stand_down


def test_no_fallback_supplied_preserves_old_behaviour():
    d = route(tax.RANGE, 0.40, specialist="range_seller")
    assert d.regime == tax.RANGE and not d.stand_down


def test_range_size_scales_with_confidence():
    """A barely-formed range earns less size than an established one."""
    lo = route(tax.RANGE, 0.20, specialist="range_seller").size_mult
    hi = route(tax.RANGE, 0.90, specialist="range_seller").size_mult
    assert 0 < lo < hi
