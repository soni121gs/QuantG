"""RAE-0 + RAE-1 — canonical taxonomy + no-lookahead intraday classifier."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core import regime_taxonomy as tax  # noqa: E402
from core.regime_classifier import classify_intraday, classify_at  # noqa: E402


def _bar(hhmm, o, h, l, c, v=0):
    return {"date": f"2025-03-03T{hhmm}:00+05:30", "open": o, "high": h, "low": l, "close": c, "volume": v}


def _up_trend_day(n=120, start=24000.0, step=6.0):
    """Efficient rising staircase — closes near the high, high efficiency."""
    bars = []
    px = start
    for i in range(n):
        hh = f"{9 + (15 + i) // 60:02d}:{(15 + i) % 60:02d}"
        o = px
        px = px + step
        bars.append(_bar(hh, o, px + 1, o - 1, px))
    return bars


def _tight_day(n=120, mid=24000.0):
    bars = []
    for i in range(n):
        hh = f"{9 + (15 + i) // 60:02d}:{(15 + i) % 60:02d}"
        # oscillate in a ~0.1% band
        c = mid + (5 if i % 2 else -5)
        bars.append(_bar(hh, mid, mid + 8, mid - 8, c))
    return bars


def _chop_day(n=120, mid=24000.0):
    bars = []
    for i in range(n):
        hh = f"{9 + (15 + i) // 60:02d}:{(15 + i) % 60:02d}"
        # big swings both ways, no net progress -> low efficiency, wide range
        c = mid + (200 if (i // 10) % 2 else -200)
        bars.append(_bar(hh, mid, mid + 260, mid - 260, c))
    return bars


# ---- RAE-0 taxonomy ----
def test_labels_frozen():
    assert tax.REGIME_LABELS == ("TREND_UP", "TREND_DOWN", "RANGE",
                                 "HIGH_VOL_CHOP", "INSIDE_QUIET", "EVENT")


def test_classify_day_trend_up():
    assert tax.classify_day(_up_trend_day()) == tax.TREND_UP


def test_classify_day_inside_quiet():
    assert tax.classify_day(_tight_day()) == tax.INSIDE_QUIET


def test_classify_day_chop():
    assert tax.classify_day(_chop_day()) == tax.HIGH_VOL_CHOP


def test_event_overlay_beats_price():
    assert tax.classify_day(_up_trend_day(), is_event=True) == tax.EVENT


# ---- RAE-1 classifier ----
def test_intraday_trend_up_confident_when_mature():
    snap = classify_intraday(_up_trend_day())
    assert snap.label == tax.TREND_UP
    assert snap.confidence > 0.5


def test_intraday_trend_low_confidence_when_immature():
    # only 10 bars in -> maturity scaling keeps confidence low even if trend-shaped
    early = _up_trend_day(n=10)
    snap = classify_intraday(early)
    # either not yet TREND, or TREND but with deliberately low confidence
    assert snap.confidence < 0.4


def test_intraday_inside_quiet():
    assert classify_intraday(_tight_day()).label == tax.INSIDE_QUIET


def test_intraday_chop():
    assert classify_intraday(_chop_day()).label == tax.HIGH_VOL_CHOP


def test_no_lookahead_classify_at_cutoff():
    day = _up_trend_day(n=120)
    # by 09:45 only ~30 bars -> lower confidence than end of day
    early = classify_at(day, "09:45")
    late = classify_at(day, "15:20")
    assert late.confidence >= early.confidence


def test_event_context_overlay():
    snap = classify_intraday(_up_trend_day(),
                             context={"event_context": {"is_event": True}})
    assert snap.label == tax.EVENT


def test_never_raises_on_junk():
    assert classify_intraday([]).label == tax.RANGE
    assert classify_intraday([_bar("09:15", 1, 1, 1, 1)]).label == tax.RANGE
