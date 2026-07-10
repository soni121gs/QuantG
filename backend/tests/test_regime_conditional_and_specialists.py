"""RAE-2 (regime-conditional judge) + RAE-3 (specialist library)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import regime_taxonomy as tax  # noqa: E402
from core.regime_conditional_oos import (  # noqa: E402
    evaluate_regime_conditional, POSITIVE_OOS, NEEDS_FORWARD_PAPER, NO_EDGE, INSUFFICIENT,
)
from core.regime_classifier import classify_intraday  # noqa: E402
from core.regime_specialists import (  # noqa: E402
    chop_stand_down, trend_long, inside_mean_revert,
)


def _bar(hhmm, o, h, l, c, v=0):
    return {"date": f"2025-03-03T{hhmm}:00+05:30", "open": o, "high": h, "low": l, "close": c, "volume": v}


def _up_trend_day(n=150, start=24000.0, step=6.0):
    bars, px = [], start
    for i in range(n):
        hh = f"{9 + (15 + i) // 60:02d}:{(15 + i) % 60:02d}"
        o = px
        px = px + step
        bars.append(_bar(hh, o, px + 1, o - 1, px))
    return bars


# ---- RAE-2 judge ----
def test_judge_positive_oos_when_both_slices_green():
    recs = ([{"date": f"2024-01-{d:02d}", "regime": tax.RANGE, "pnl": 100} for d in range(1, 16)]
            + [{"date": f"2025-01-{d:02d}", "regime": tax.RANGE, "pnl": 80} for d in range(1, 16)])
    v = evaluate_regime_conditional(recs, tax.RANGE, min_trades=20)
    assert v.verdict == POSITIVE_OOS


def test_judge_thin_sample_is_forward_paper_not_veto():
    recs = [{"date": "2024-01-01", "regime": tax.TREND_UP, "pnl": 3000},
            {"date": "2025-01-01", "regime": tax.TREND_UP, "pnl": 500}]
    v = evaluate_regime_conditional(recs, tax.TREND_UP, min_trades=20)
    assert v.verdict == NEEDS_FORWARD_PAPER   # positive but n<20 → forward-paper, NOT killed


def test_judge_no_edge_when_negative():
    recs = [{"date": f"2024-01-{d:02d}", "regime": tax.RANGE, "pnl": -50} for d in range(1, 25)]
    recs += [{"date": f"2025-01-{d:02d}", "regime": tax.RANGE, "pnl": -30} for d in range(1, 25)]
    assert evaluate_regime_conditional(recs, tax.RANGE).verdict == NO_EDGE


def test_judge_off_regime_giveback_flagged():
    recs = ([{"date": f"2024-01-{d:02d}", "regime": tax.RANGE, "pnl": 100} for d in range(1, 25)]
            + [{"date": f"2025-01-{d:02d}", "regime": tax.RANGE, "pnl": 90} for d in range(1, 25)]
            + [{"date": f"2025-02-{d:02d}", "regime": tax.TREND_UP, "pnl": -500} for d in range(1, 10)])
    v = evaluate_regime_conditional(recs, tax.RANGE)
    assert v.off_regime["n"] == 9
    assert any("OFF-regime" in r for r in v.reasons)


def test_judge_stand_down_counts_not_pnl():
    recs = [{"date": "2024-01-01", "regime": tax.TREND_UP, "pnl": None},
            {"date": "2025-01-01", "regime": tax.TREND_UP, "pnl": None}]
    v = evaluate_regime_conditional(recs, tax.TREND_UP)
    assert v.verdict == INSUFFICIENT and v.stood_down_days == 2


# ---- RAE-3 specialists ----
def test_chop_stand_down_true_on_chop_and_event():
    from core.regime_classifier import RegimeSnapshot
    assert chop_stand_down(RegimeSnapshot(tax.HIGH_VOL_CHOP, 0.8))
    assert chop_stand_down(RegimeSnapshot(tax.EVENT, 0.9))
    assert not chop_stand_down(RegimeSnapshot(tax.RANGE, 0.4))


def test_trend_long_fires_on_confident_uptrend():
    pnl = trend_long(_up_trend_day(), daily_trend_up=True, conf_min=0.5)
    assert pnl is not None   # took the trade on a clean up-trend day


def test_trend_long_stands_down_when_daily_disagrees():
    assert trend_long(_up_trend_day(), daily_trend_up=False, conf_min=0.5) is None


def test_trend_long_stands_down_when_confidence_below_gate():
    # impossible gate → never fires even on a trend day (precision knob works)
    assert trend_long(_up_trend_day(), daily_trend_up=True, conf_min=1.01) is None


def test_specialists_never_raise_on_short_input():
    assert trend_long([], daily_trend_up=True) is None
    assert inside_mean_revert([]) is None
