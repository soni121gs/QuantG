"""IMD-08 — intraday OOS metrics + verdict (pure logic)."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.intraday_options_oos import (  # noqa: E402
    CANDIDATE_EDGE,
    DATA_QUALITY_FAIL,
    FRAGILE,
    INSUFFICIENT_DATA,
    NO_EDGE_NEGATIVE,
    evaluate_strategy,
    metrics,
    walk_forward,
)


def _trades(spec):
    """spec: list of (date, net_pnl)."""
    out = []
    for i, (date, pnl) in enumerate(spec):
        out.append({
            "date": date, "net_pnl": pnl,
            "entry_ts": f"{date}T09:15:00+05:30", "exit_ts": f"{date}T09:35:00+05:30",
            "mfe": abs(pnl) + 5, "mae": -abs(pnl),
        })
    return out


def _many(months, per_month, pnl):
    spec = []
    for m in months:
        for d in range(per_month):
            spec.append((f"{m}-{d + 1:02d}", pnl))
    return _trades(spec)


def test_metrics_basic():
    m = metrics(_trades([("2025-01-06", 100), ("2025-01-07", -50), ("2025-01-08", 200)]))
    assert m["trades"] == 3
    assert m["net_pnl"] == 250.0
    assert m["win_rate"] == round(2 / 3, 4)
    assert m["profit_factor"] == round(300 / 50, 3)
    assert m["avg_hold_min"] == 20.0


def test_drawdown_negative():
    m = metrics(_trades([("2025-01-06", 100), ("2025-01-07", -300), ("2025-01-08", 50)]))
    assert m["max_drawdown"] == -300.0


def test_insufficient_data_thin_sample():
    r = evaluate_strategy("QG-O5", _trades([("2025-01-06", 100), ("2025-01-07", 50)]))
    assert r["verdict"] == INSUFFICIENT_DATA


def test_data_quality_fail():
    trades = _many(["2025-01", "2025-02", "2025-03", "2025-04"], 10, 100)
    r = evaluate_strategy("QG-O5", trades, missing_rate=0.5)
    assert r["verdict"] == DATA_QUALITY_FAIL


def test_negative_expectancy():
    trades = _many(["2025-01", "2025-02", "2025-03", "2025-04"], 10, -100)
    r = evaluate_strategy("QG-O6", trades)
    assert r["verdict"] == NO_EDGE_NEGATIVE


def test_candidate_edge_robust_plateau():
    # positive every month incl. the held-out latest month
    trades = _many(["2025-01", "2025-02", "2025-03", "2025-04"], 10, 150)
    r = evaluate_strategy("QG-O5", trades)
    assert r["verdict"] == CANDIDATE_EDGE
    assert r["oos"]["expectancy"] > 0
    assert r["green_month_pct"] == 1.0


def test_fragile_when_oos_month_negative():
    # profitable in-sample, but the held-out latest month loses -> not robust
    trades = _many(["2025-01", "2025-02", "2025-03"], 10, 200)
    trades += _trades([(f"2025-04-{d + 1:02d}", -300) for d in range(10)])
    r = evaluate_strategy("QG-O7", trades)
    assert r["overall"]["expectancy"] > 0
    assert r["oos"]["expectancy"] < 0
    assert r["verdict"] == FRAGILE


def test_walk_forward_holds_out_latest_month():
    trades = _many(["2025-01", "2025-02", "2025-03"], 5, 100)
    wf = walk_forward(trades, oos_months=1)
    assert wf["oos_months"] == ["2025-03"]
    assert wf["oos"]["trades"] == 5
    assert wf["train"]["trades"] == 10
