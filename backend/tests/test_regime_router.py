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


def test_trend_size_scales_with_confidence(monkeypatch):
    # magnitude scaling only when SCORE_SIZE_NEUTRAL=false (M5 disabled it by default)
    monkeypatch.setenv("SCORE_SIZE_NEUTRAL", "false")
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

def test_range_fallthrough_is_cross_checked_against_the_coarse_regime():
    """RANGE is the classifier's no-signature default AND the sellers' home, so it
    is the one label that gets cross-checked. 2026-07-22: fine said RANGE/0.40 all
    morning while the mature coarse read said TREND_DOWN."""
    d = route(tax.RANGE, 0.40, specialist="range_seller", fallback_regime=tax.TREND_DOWN)
    assert d.regime == tax.TREND_DOWN
    assert d.stand_down and d.size_mult == 0.0
    assert any("cross-checking" in r for r in d.reasons)


def test_affirmative_fine_label_is_trusted_and_not_cross_checked():
    """An affirmative TREND/CHOP/INSIDE call is a real detection — only the RANGE
    default is second-guessed."""
    d = route(tax.TREND_UP, 0.95, specialist="trend_delta1", fallback_regime=tax.RANGE)
    assert d.regime == tax.TREND_UP and not d.stand_down


def test_no_fallback_supplied_preserves_old_behaviour():
    d = route(tax.RANGE, 0.40, specialist="range_seller")
    assert d.regime == tax.RANGE and not d.stand_down


def test_range_size_scales_with_confidence(monkeypatch):
    """A barely-formed range earns less size than an established one — ONLY in the
    legacy magnitude-scaling mode (M5 disabled it by default)."""
    monkeypatch.setenv("SCORE_SIZE_NEUTRAL", "false")
    lo = route(tax.RANGE, 0.20, specialist="range_seller").size_mult
    hi = route(tax.RANGE, 0.90, specialist="range_seller").size_mult
    assert 0 < lo < hi


def test_mature_range_earns_full_size(monkeypatch):
    """P5-R3 (legacy scaling path): a mature RANGE tops out at RANGE_BASE_CONF (0.40)
    confidence. The seller-size scaling references that ceiling, not FINE_MIN_CONF, so
    an established range earns full size. Only meaningful with magnitude scaling on."""
    monkeypatch.setenv("SCORE_SIZE_NEUTRAL", "false")
    from core.regime_router import SELLER_SIZE_CONF_REF, REGIME_SIZE
    d = route(tax.RANGE, SELLER_SIZE_CONF_REF, specialist="range_seller")
    assert not d.stand_down
    assert d.size_mult == REGIME_SIZE[tax.RANGE]  # unthrottled at the mature ceiling


def test_default_neutral_size_is_flat_across_confidence():
    """M5 default (SCORE_SIZE_NEUTRAL=true): confidence is inverted vs P&L, so size is
    NOT scaled by its magnitude — a seller in its owned regime gets flat base size at
    any confidence. Confidence still gates stand-down/trend precision categorically."""
    lo = route(tax.RANGE, 0.20, specialist="range_seller")
    hi = route(tax.RANGE, 0.90, specialist="range_seller")
    assert not lo.stand_down and not hi.stand_down
    assert lo.size_mult == hi.size_mult == REGIME_SIZE_FLAT_RANGE()


def REGIME_SIZE_FLAT_RANGE():
    from core.regime_router import REGIME_SIZE
    return REGIME_SIZE[tax.RANGE]


# --- P5-R1 (2026-07-23): the fallback must only ever REDUCE risk -------------
# The first version of the cross-check rewrote the regime label at the top of
# route(), above every protective guard, and broke three things at once.

def test_long_vol_still_owns_chop_when_a_range_fallback_is_supplied(monkeypatch):
    """Regression guard for the 2026-07-21 'IDX Long-Gamma inversion', which the
    first fallback implementation reintroduced: rewriting CHOP->RANGE meant the
    long-vol guard never fired and the sleeve stood down on the one regime it owns."""
    monkeypatch.setenv("RAE_CHOP_STANDDOWN", "true")
    d = route(tax.HIGH_VOL_CHOP, 0.40, specialist=tax.LONG_VOL_ROLE,
              fallback_regime=tax.RANGE)
    assert not d.stand_down and d.size_mult > 0


def test_fallback_cannot_defeat_the_chop_veto(monkeypatch):
    """A low-confidence CHOP read must not be laundered into RANGE so sellers trade it."""
    monkeypatch.setenv("RAE_CHOP_STANDDOWN", "true")
    d = route(tax.HIGH_VOL_CHOP, 0.40, specialist="range_seller", fallback_regime=tax.RANGE)
    assert d.stand_down


def test_low_conf_trend_defers_to_mature_coarse_range():
    """2026-07-23: a LOW-confidence fine trend is noise — when the mature coarse
    regime says RANGE (the seller's home), the seller trades on the coarse read
    rather than being suppressed. (The real 07-22 loss had coarse genuinely
    TREND_DOWN — that still stands down, covered by the range-fallthrough test.)"""
    d = route(tax.TREND_DOWN, 0.30, specialist="range_seller", fallback_regime=tax.RANGE)
    assert not d.stand_down
    # but a CONFIDENT fine trend is real and still stands the seller down:
    assert route(tax.TREND_DOWN, 0.90, specialist="range_seller",
                 fallback_regime=tax.RANGE).stand_down


def test_cross_check_never_authorizes_what_the_fine_read_refused():
    """The more conservative decision always wins."""
    permissive = route(tax.RANGE, 0.40, specialist="range_seller", fallback_regime=tax.RANGE)
    checked = route(tax.RANGE, 0.40, specialist="range_seller", fallback_regime=tax.TREND_DOWN)
    assert checked.size_mult <= permissive.size_mult


# --- 2026-07-23: symmetric rescue — low-confidence fine trend must not stand a
# seller down when the mature coarse regime is its home (QG-O1 over-block fix) ---

def test_low_conf_fine_trend_defers_to_owned_coarse_regime():
    """QG-O1: fine=TREND_UP at 0.03 (noise) stood the range_seller down while coarse
    was RANGE (its home). It must trade, not be suppressed by noise."""
    d = route(tax.TREND_UP, 0.03, specialist="range_seller", fallback_regime=tax.RANGE)
    assert not d.stand_down and d.size_mult > 0


def test_high_conf_fine_trend_still_stands_the_seller_down():
    """A CONFIDENT trend is real — the rescue must not fire above FINE_MIN_CONF."""
    d = route(tax.TREND_UP, 0.95, specialist="range_seller", fallback_regime=tax.RANGE)
    assert d.stand_down


def test_rescue_never_suppresses_a_trade_only_enables_one():
    """The rescue only turns a stand-down into a trade — never the reverse."""
    # fine says trade (owns INSIDE), low conf, coarse RANGE — must still trade
    d = route(tax.INSIDE_QUIET, 0.10, specialist="range_seller", fallback_regime=tax.RANGE)
    assert not d.stand_down


def test_rescue_does_not_reintroduce_the_longvol_inversion():
    """long_vol owns CHOP and is never stood down there, so the rescue is moot — it
    must keep trading chop (guard against re-breaking the 2026-07-21 inversion)."""
    d = route(tax.HIGH_VOL_CHOP, 0.40, specialist=tax.LONG_VOL_ROLE, fallback_regime=tax.RANGE)
    assert not d.stand_down
