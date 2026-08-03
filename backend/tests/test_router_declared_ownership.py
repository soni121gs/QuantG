"""Regime ownership must come from the strategy's DECLARED config, not a hardcoded
role list — and declaring a regime must not bypass the protective vetoes.

2026-08-03 audit: `visual_config.options.owned_regimes` was set on every seeded
specialist and NOTHING read it. Ownership was decided purely by matching the role
name against `regime_taxonomy.REGIME_OWNER`, so two LIVE strategies whose roles are
absent from that map stood down in ALL SIX regimes and could never trade:

  * `slow_premium_hte`  — the hold-to-expiry sleeve seeded 2026-08-03,
  * `tail_hedge`        — which declared it owned all six.

Same decorative-config trap as `target_dte_days` (§21.5): the row exists, looks
armed, and silently never trades.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import regime_taxonomy as tax
from core.regime_router import route

ALL = ["RANGE", "INSIDE_QUIET", "HIGH_VOL_CHOP", "TREND_UP", "TREND_DOWN", "EVENT"]
SELLER = ["RANGE", "INSIDE_QUIET"]


def _conf(regime):
    return 0.95 if regime in ("TREND_UP", "TREND_DOWN") else 0.45


def _d(regime, **kw):
    return route(regime, _conf(regime), fallback_regime=regime, **kw)


# ── the two dead strategies ──────────────────────────────────────────────────

def test_hold_to_expiry_sleeve_could_never_trade_without_declared_ownership():
    """The regression itself: an unknown role stands down everywhere."""
    for regime in ALL:
        assert _d(regime, specialist="slow_premium_hte").stand_down


def test_hold_to_expiry_sleeve_trades_its_declared_regimes():
    for regime in SELLER:
        d = _d(regime, specialist="slow_premium_hte",
               owned_regimes=SELLER, structure="credit_spread")
        assert not d.stand_down and d.size_mult > 0, d.as_dict()


def test_tail_hedge_is_on_in_every_regime_it_declares():
    """A hedge that stands down on chop/EVENT is not a hedge."""
    for regime in ALL:
        d = _d(regime, specialist="tail_hedge", owned_regimes=ALL, structure="debit_spread")
        assert not d.stand_down, (regime, d.as_dict())


# ── declaring a regime is NOT a bypass ───────────────────────────────────────

def test_a_premium_seller_may_never_buy_its_way_into_chop_or_event():
    """The whole point of the chop/EVENT veto: the fat tail is against a seller.
    Declaring ownership must not override that."""
    for regime in ("HIGH_VOL_CHOP", "EVENT"):
        d = _d(regime, specialist="range_seller", owned_regimes=ALL, structure="credit_spread")
        assert d.stand_down, (regime, d.as_dict())


def test_trend_confidence_gate_still_applies_to_a_declared_owner():
    """Trend calls are 16% precise (the 498-day study) — a declaration is not evidence."""
    d = route("TREND_UP", 0.30, specialist="trend_delta1", fallback_regime="TREND_UP",
              owned_regimes=["TREND_UP", "TREND_DOWN"], structure="single_leg")
    assert d.stand_down and "confidence" in " ".join(d.reasons)


def test_declared_ownership_still_stands_down_off_regime():
    d = _d("TREND_UP", specialist="range_seller", owned_regimes=SELLER,
           structure="credit_spread")
    assert d.stand_down and "does not own" in " ".join(d.reasons)


# ── the working book must not move ───────────────────────────────────────────

def test_existing_seller_routing_is_byte_identical():
    """No behaviour change for the strategies that were already routing correctly."""
    for regime in ALL:
        before = _d(regime, specialist="range_seller")
        after = _d(regime, specialist="range_seller",
                   owned_regimes=SELLER, structure="credit_spread")
        assert before.stand_down == after.stand_down
        assert before.size_mult == after.size_mult


def test_long_vol_keeps_chop_even_if_untagged():
    """`long_vol` must survive on its role name alone — it may be seeded untagged."""
    d = _d("HIGH_VOL_CHOP", specialist=tax.LONG_VOL_ROLE)
    assert not d.stand_down and d.size_mult > 0


def test_untagged_legacy_strategy_uses_the_role_map():
    for regime in SELLER:
        assert not _d(regime, specialist="range_seller").stand_down
    assert _d("TREND_UP", specialist="range_seller").stand_down


# ── input hygiene ────────────────────────────────────────────────────────────

def test_garbage_owned_regimes_falls_back_to_the_role_map():
    """Junk must not silently open or close everything."""
    for junk in ([], ["NOT_A_REGIME"], "", None, 42):
        d = _d("RANGE", specialist="range_seller", owned_regimes=junk,
               structure="credit_spread")
        assert not d.stand_down, junk
        assert _d("TREND_UP", specialist="range_seller", owned_regimes=junk,
                  structure="credit_spread").stand_down, junk


def test_owned_regimes_accepts_a_bare_string_and_is_case_insensitive():
    d = _d("RANGE", specialist="slow_premium_hte", owned_regimes="range",
           structure="credit_spread")
    assert not d.stand_down
