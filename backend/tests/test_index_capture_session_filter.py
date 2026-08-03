"""Regression guards for the 2026-08-03 fine-regime corruption.

Measured that day: 210 of 213 signals were skipped `RAE_ROUTER_STAND_DOWN`.
Running the SAME `classify_intraday` on the real Upstox bars returned
INSIDE_QUIET/RANGE/INSIDE_QUIET — the premium sellers' owned regimes — while
`market_regime_state` held TREND_UP/TREND_UP/HIGH_VOL_CHOP. The classifier was
correct; its INPUT (the live capture buffer) was corrupt, two ways:

  1. The feed connected 08:32 IST, 43 min before the open, and `on_tick` had no
     session filter. Pre-open prints carry the PREVIOUS close, so bars[0].open
     became Friday's close and `ret_pct` turned gap-inclusive: NIFTY's flat
     +0.13% session read as +0.98% -> TREND_UP at confidence ~0.90.
  2. `ltp in (None, "")` admits 0.0. One zero print (SENSEX, pre-open, before BSE
     computes the index) sets the day's low to zero -> rng_pct ~100% ->
     HIGH_VOL_CHOP at confidence 1.0, the router's explicit stand-down.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.live_index_capture import LiveIndexCapture
from core.regime_classifier import classify_intraday

NIFTY_KEY = "NSE_INDEX|Nifty 50"
SENSEX_KEY = "BSE_INDEX|SENSEX"


def _tick(ts: str, ltp):
    return {"received_at": ts, "ltp": ltp}


def _cap():
    class _NoStore:
        def write_day(self, *a, **k):  # pragma: no cover - never called here
            raise AssertionError("write_day must not run in these tests")

    return LiveIndexCapture(store=_NoStore())


def _hhmm(minute_of_day: int) -> str:
    return f"{minute_of_day // 60:02d}:{minute_of_day % 60:02d}"


def test_preopen_ticks_are_not_aggregated():
    cap = _cap()
    start = 8 * 60 + 32           # 08:32 — the real 2026-08-03 connect time
    count = 9 * 60 + 15 - start   # ..09:14, the last pre-open minute
    for i in range(count):
        cap.on_tick(NIFTY_KEY, _tick(f"2026-08-03T{_hhmm(start + i)}:10+05:30", 24366.70))
    assert cap.health()["ticks_seen"] == 0
    assert cap.health()["rejected_outside_session"] == count
    assert cap.snapshot_minutes("NIFTY", include_open=True) == []


def test_in_session_ticks_are_aggregated():
    cap = _cap()
    for i in range(5):
        cap.on_tick(NIFTY_KEY, _tick(f"2026-08-03T09:{15 + i:02d}:10+05:30", 24572.0 + i))
    assert cap.health()["ticks_seen"] == 5
    assert cap.health()["rejected_outside_session"] == 0
    assert len(cap.snapshot_minutes("NIFTY", include_open=True)) >= 4


def test_zero_price_tick_is_rejected():
    """0.0 passes `ltp in (None, "")` — it must not reach the aggregator."""
    cap = _cap()
    cap.on_tick(SENSEX_KEY, _tick("2026-08-03T09:20:00+05:30", 78883.34))
    cap.on_tick(SENSEX_KEY, _tick("2026-08-03T09:21:00+05:30", 0.0))
    cap.on_tick(SENSEX_KEY, _tick("2026-08-03T09:22:00+05:30", -5.0))
    assert cap.health()["rejected_nonpositive_price"] == 2
    lows = [b["low"] for b in cap.snapshot_minutes("SENSEX", include_open=True)]
    assert lows and min(lows) > 0


def test_sensex_uses_the_bse_close():
    """BSE F&O stays at 15:30; 15:35 is inside NSE's session but not BSE's."""
    cap = _cap()
    cap.on_tick(SENSEX_KEY, _tick("2026-08-03T15:35:00+05:30", 78883.34))
    assert cap.health()["ticks_seen"] == 0
    cap.on_tick(NIFTY_KEY, _tick("2026-08-03T15:35:00+05:30", 24605.35))
    assert cap.health()["ticks_seen"] == 1


# ── the end-to-end signature: corrupt input flips the router's verdict ────────

def _session_bars():
    """A quiet, rangebound NIFTY session — the sellers' owned regime."""
    out = []
    for i in range(120):
        px = 24572.0 + (i % 7) * 4.0          # ~0.11% wander, no direction
        ts = f"2026-08-03T{9 + (15 + i) // 60:02d}:{(15 + i) % 60:02d}:00+05:30"
        out.append({"date": ts, "open": px, "high": px + 2, "low": px - 2,
                    "close": px, "volume": 0})
    return out


def test_clean_session_classifies_as_a_seller_regime():
    snap = classify_intraday(_session_bars())
    assert snap.label in ("INSIDE_QUIET", "RANGE"), snap.as_dict()


def test_preopen_contamination_would_flip_it_to_trend_up():
    """Pins the mechanism: prepending pre-open bars at the previous close turns a
    flat session into TREND_UP. This is what the session filter now prevents."""
    pre = [{"date": f"2026-08-03T{_hhmm(8 * 60 + 32 + i)}:00+05:30", "open": 24366.70,
            "high": 24366.70, "low": 24366.70, "close": 24366.70, "volume": 0}
           for i in range(9 * 60 + 15 - (8 * 60 + 32))]
    snap = classify_intraday(pre + _session_bars())
    assert snap.label == "TREND_UP" and snap.confidence > 0.6, snap.as_dict()


def test_one_zero_bar_would_flip_it_to_high_vol_chop():
    """Pins the second mechanism: a single 0.0 print maxes out the chop range."""
    bars = _session_bars()
    bars.insert(3, {"date": "2026-08-03T09:18:30+05:30", "open": 0.0, "high": 0.0,
                    "low": 0.0, "close": 0.0, "volume": 0})
    snap = classify_intraday(bars)
    assert snap.label == "HIGH_VOL_CHOP" and snap.confidence == 1.0, snap.as_dict()
