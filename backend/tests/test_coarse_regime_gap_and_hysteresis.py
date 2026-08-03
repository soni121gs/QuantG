"""The coarse regime must not call an overnight GAP an intraday TREND, and must not
flip labels while price oscillates on its VWAP.

Measured 2026-08-03: NIFTY gapped +0.85% and then went nowhere — session move +0.13%,
range 0.38%, a textbook intraday range day — yet `intraday_return_pct` (measured from
the PREVIOUS CLOSE) stayed above the 0.5% trend threshold all session, so the coarse
regime read TREND_UP. That label is the conservative cross-check the RAE router applies
to a RANGE fine-read, so a gap-and-flat day vetoed every premium seller.

Separately, SENSEX flipped TREND_UP<->RANGE 16 times in 20 minutes at a return of
0.87-0.88% — the three price-vs-VWAP/EMA conditions have no deadband.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market_regime as mr


def _candles(prev_close, day_opens_at, path, day="2026-08-03"):
    """One prior-day bar at `prev_close`, then today's closes following `path`."""
    out = [{"date": "2026-07-31 15:25", "open": prev_close, "high": prev_close,
            "low": prev_close, "close": prev_close, "volume": 1}]
    px = day_opens_at
    for i, px in enumerate(path):
        hh, mm = 9 + (15 + i * 5) // 60, (15 + i * 5) % 60
        out.append({"date": f"{day} {hh:02d}:{mm:02d}", "open": px, "high": px * 1.0005,
                    "low": px * 0.9995, "close": px, "volume": 1})
    return out


def _gap_up_then_flat():
    """The real 2026-08-03 NIFTY shape: +0.85% gap, then a 0.13% drift."""
    base = 24572.70
    return _candles(24366.70, base, [base * (1 + 0.0013 * i / 40) for i in range(41)])


def test_a_gap_then_flat_session_is_not_a_trend():
    r = mr.compute_regime_from_data("NIFTY", _gap_up_then_flat())
    assert r["regime"] != "TREND_UP", r
    assert r["intraday_return_pct"] > 0.5      # gap-inclusive is still large…
    assert r["session_return_pct"] < 0.5       # …but the session barely moved


def test_both_returns_are_reported():
    r = mr.compute_regime_from_data("NIFTY", _gap_up_then_flat())
    assert "intraday_return_pct" in r and "session_return_pct" in r
    assert r["intraday_return_pct"] != r["session_return_pct"]


def test_a_real_intraday_trend_still_reads_trend_up():
    """No gap, but price grinds up 1.2% through the session — that IS a trend."""
    base = 24000.0
    r = mr.compute_regime_from_data(
        "NIFTY", _candles(base, base, [base * (1 + 0.012 * i / 40) for i in range(41)]))
    assert r["regime"] == "TREND_UP", r


def test_crash_guard_still_uses_the_gap_inclusive_return():
    """A -2% gap-down IS a crash day even if price is flat afterwards. The fat-tail
    guard must stay conservative — this is the half that must NOT change."""
    base = 24000.0
    gapped = base * 0.98
    r = mr.compute_regime_from_data(
        "NIFTY", _candles(base, gapped, [gapped] * 41))
    assert r["regime"] == "CRASH", r
    assert r["long_entries_allowed"] is False


def test_meltup_guard_still_uses_the_gap_inclusive_return():
    base = 24000.0
    gapped = base * 1.02
    r = mr.compute_regime_from_data("NIFTY", _candles(base, gapped, [gapped] * 41))
    assert r["regime"] == "MELTUP", r


# ── hysteresis ───────────────────────────────────────────────────────────────

def _borderline_trend():
    """Session move parked just above the entry threshold."""
    base = 24000.0
    target = base * (1 + (mr.TREND_THRESHOLD_PCT - 0.02) / 100.0)
    path = [base + (target - base) * (i / 40) for i in range(41)]
    return _candles(base, base, path)


def test_an_established_trend_is_not_dropped_on_a_hair():
    """Just under the entry threshold: a fresh read is RANGE, but an established
    TREND_UP holds — that is the deadband that stops the 16-flips-in-20-minutes."""
    candles = _borderline_trend()
    fresh = mr.compute_regime_from_data("NIFTY", candles)
    held = mr.compute_regime_from_data("NIFTY", candles, previous_regime="TREND_UP")
    assert fresh["regime"] == "RANGE", fresh
    assert held["regime"] == "TREND_UP", held


def test_hysteresis_does_not_invent_a_trend_from_nothing():
    """A flat day must stay RANGE even if the previous label was TREND_UP —
    the buffer widens the exit, it must never widen the ENTRY."""
    base = 24000.0
    flat = _candles(base, base, [base] * 41)
    assert mr.compute_regime_from_data(
        "NIFTY", flat, previous_regime="TREND_UP")["regime"] == "RANGE"


def test_previous_regime_is_optional_and_default_is_unchanged():
    candles = _gap_up_then_flat()
    assert (mr.compute_regime_from_data("NIFTY", candles)["regime"]
            == mr.compute_regime_from_data("NIFTY", candles, previous_regime=None)["regime"])
