"""Tests for RES-2 — core/market_context.py (the Market Intelligence Engine).

Verifies the five signals as pure functions and the seller_decision synthesis.
The pure-function design is item G: these same functions run live and in the
backtester, so testing them once proves both paths.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.market_context import (
    realized_vol_pct,
    vol_edge,
    vol_state,
    chain_intel,
    event_context,
    build_context,
)


# ── fixtures ────────────────────────────────────────────────────────────────────

def _regime_candles(prev_close, today_closes, day_prev="2025-01-01", day="2025-01-02"):
    """Two-day 5-min candle series for compute_regime_from_data. Flat H=L=C bars,
    volume 1, so VWAP is the running mean of closes."""
    out = [{"date": f"{day_prev} 15:25:00", "open": prev_close, "high": prev_close,
            "low": prev_close, "close": prev_close, "volume": 1}]
    for i, c in enumerate(today_closes):
        out.append({"date": f"{day} 09:{15 + i * 5:02d}:00", "open": c, "high": c,
                    "low": c, "close": c, "volume": 1})
    return out


def _flat_closes(n=21, start=100.0, step_pct=0.0002):
    """Near-flat daily closes → tiny realized vol (premium looks rich vs it)."""
    closes = [start]
    for _ in range(n - 1):
        closes.append(round(closes[-1] * (1 + step_pct), 4))
    return closes


# ── A) realized vol + vol edge ──────────────────────────────────────────────────

def test_realized_vol_none_on_thin_data():
    assert realized_vol_pct([]) is None
    assert realized_vol_pct([100.0]) is None


def test_realized_vol_positive_and_scales():
    calm = realized_vol_pct([100, 100.1, 100.0, 100.1, 100.0])
    wild = realized_vol_pct([100, 105, 96, 107, 95])
    assert calm is not None and wild is not None
    assert wild > calm > 0


def test_vol_edge_rich_when_iv_far_above_rv():
    ve = vol_edge(15.0, _flat_closes())
    assert ve["rich"] is True and ve["edge"] > 0
    assert ve["iv"] == 15.0 and ve["rv"] is not None


def test_vol_edge_thin_when_iv_near_rv():
    closes = [100, 103, 97, 104, 96, 105, 95, 106, 94, 107, 93] * 2  # very wild → high rv
    ve = vol_edge(12.0, closes)
    assert ve["rich"] is False


def test_vol_edge_unknown_when_missing():
    assert vol_edge(None, _flat_closes())["rich"] is None
    assert vol_edge(15.0, [])["rich"] is None


# ── B) vol state ────────────────────────────────────────────────────────────────

def test_vol_state_stormy_on_expansion():
    # 16 calm closes then 5 violent ones → short RV >> long RV.
    closes = _flat_closes(n=16) + [100.0, 106.0, 94.0, 107.0, 93.0]
    vs = vol_state(closes)
    assert vs["state"] == "STORMY"


def test_vol_state_unknown_on_thin_data():
    assert vol_state([100.0])["state"] == "UNKNOWN"


# ── C) chain intelligence ───────────────────────────────────────────────────────

def _chain():
    return [
        {"strike": 24800, "option_type": "CE", "oi": 1_000, "iv": 12.0},
        {"strike": 25000, "option_type": "CE", "oi": 5_000, "iv": 11.0},   # call wall
        {"strike": 24600, "option_type": "PE", "oi": 8_000, "iv": 15.0},   # put wall
        {"strike": 24400, "option_type": "PE", "oi": 2_000, "iv": 16.0},
    ]


def test_chain_intel_pcr_walls_and_skew():
    ci = chain_intel(_chain(), spot=24700)
    assert ci["call_wall"] == 25000 and ci["put_wall"] == 24600
    assert ci["pcr"] == round(10_000 / 6_000, 3)
    # OTM puts (strike<spot) IV avg 15.5 vs OTM calls (strike>spot) IV 11 → skew +.
    assert ci["skew"] > 0 and ci["richer_side"] == "PE"


def test_chain_intel_empty_safe():
    ci = chain_intel([], spot=None)
    assert ci["pcr"] is None and ci["skew"] is None and ci["richer_side"] is None


# ── E) event calendar ───────────────────────────────────────────────────────────

def test_event_context_blocks_expiry_and_event():
    assert event_context("2025-01-02", expiry_dates=["2025-01-02"])["block_new_sells"] is True
    assert event_context("2025-01-02", event_dates=["2025-01-02"])["is_event"] is True
    assert event_context("2025-01-03", expiry_dates=["2025-01-02"])["block_new_sells"] is False


# ── F) synthesis: build_context / seller_decision ───────────────────────────────

def test_seller_allowed_in_range_rich_calm_with_side():
    # Small +0.3% drift, current above VWAP → RANGE / BULL bias → sell PUT spread.
    candles = _regime_candles(100.0, [100.2, 100.3, 100.3])
    ctx = build_context(index="NIFTY", candles=candles, daily_closes=_flat_closes(),
                        iv_pct=15.0, spot=24700, chain=_chain(), date_str="2025-01-02")
    assert ctx["regime"]["regime"] == "RANGE"
    assert ctx["seller_decision"]["allow_sell"] is True
    assert ctx["seller_decision"]["side"] == "PE"


def test_seller_blocked_in_trend_down():
    candles = _regime_candles(100.0, [99.7, 99.5, 99.3, 99.0])  # −1% intraday
    ctx = build_context(index="NIFTY", candles=candles, daily_closes=_flat_closes(),
                        iv_pct=15.0, spot=24700, chain=_chain(), date_str="2025-01-02")
    assert ctx["regime"]["regime"] == "TREND_DOWN"
    assert ctx["seller_decision"]["allow_sell"] is False
    assert any("not sell-safe" in r for r in ctx["seller_decision"]["reasons"])


def test_seller_blocked_when_edge_thin():
    candles = _regime_candles(100.0, [100.2, 100.3, 100.3])
    wild = [100, 106, 94, 107, 93, 108, 92, 109, 91, 110, 90] * 2
    ctx = build_context(index="NIFTY", candles=candles, daily_closes=wild,
                        iv_pct=11.0, spot=24700, chain=_chain(), date_str="2025-01-02")
    assert ctx["seller_decision"]["allow_sell"] is False


def test_seller_blocked_on_event_day():
    candles = _regime_candles(100.0, [100.2, 100.3, 100.3])
    ctx = build_context(index="NIFTY", candles=candles, daily_closes=_flat_closes(),
                        iv_pct=15.0, spot=24700, chain=_chain(), date_str="2025-01-02",
                        expiry_dates=["2025-01-02"])
    assert ctx["seller_decision"]["allow_sell"] is False
    assert any("EVENT" in r or "event" in r for r in ctx["seller_decision"]["reasons"])


def test_order_flow_imbalance_attached():
    candles = _regime_candles(100.0, [100.2, 100.3, 100.3])
    ctx = build_context(index="NIFTY", candles=candles, daily_closes=_flat_closes(),
                        iv_pct=15.0, spot=24700, chain=_chain(), date_str="2025-01-02",
                        contract={"tbq": 8000, "tsq": 2000})
    assert ctx["order_flow"]["imbalance"] == round((8000 - 2000) / 10000, 3)
