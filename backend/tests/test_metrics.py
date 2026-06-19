"""Unit tests for core/metrics.py — pure, broker-free, no DB."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.metrics import compute_metrics, grade


def test_empty():
    m = compute_metrics([])
    assert m["total_trades"] == 0
    assert m["sharpe"] == 0.0
    assert m["equity_curve"] == []


def test_basic_win_loss_counts():
    m = compute_metrics([100.0, -50.0, 100.0, -50.0])
    assert m["total_trades"] == 4
    assert m["wins"] == 2 and m["losses"] == 2
    assert m["win_rate"] == 0.5
    assert m["gross_profit"] == 200.0
    assert m["gross_loss"] == 100.0
    assert m["profit_factor"] == 2.0
    assert m["avg_win"] == 100.0
    assert m["avg_loss"] == 50.0
    assert m["payoff_ratio"] == 2.0
    assert m["expectancy"] == 25.0  # +100 net over 4 trades
    assert m["total_pnl"] == 100.0
    assert m["best_trade"] == 100.0
    assert m["worst_trade"] == -50.0


def test_all_wins_profit_factor_capped_and_sortino_99():
    m = compute_metrics([100.0, 100.0, 100.0])
    assert m["win_rate"] == 1.0
    assert m["profit_factor"] == 99.0   # no losses
    assert m["max_drawdown_pct"] == 0.0  # monotonic up
    assert m["sortino"] == 99.0          # no downside, positive mean


def test_all_losses_negative_expectancy_grade_f():
    m = compute_metrics([-10.0, -20.0, -30.0, -5.0, -5.0])
    assert m["expectancy"] < 0
    assert m["profit_factor"] == 0.0
    assert grade(m) == "F"


def test_drawdown_computation():
    # equity: 100100, 99900, 99950 on base 100000; peak 100100; trough 99900
    m = compute_metrics([100.0, -200.0, 50.0], starting_capital=100_000.0)
    # max dd = (100100 - 99900) / 100100 = 0.19980% -> ~0.2
    assert abs(m["max_drawdown_pct"] - 0.2) < 0.05
    assert m["equity_curve"] == [100100.0, 99900.0, 99950.0]


def test_sharpe_positive_when_consistently_profitable():
    m = compute_metrics([50.0, 60.0, 40.0, 55.0, 45.0])
    assert m["sharpe"] > 0
    assert m["expectancy"] == 50.0


def test_grade_insufficient_under_five_trades():
    assert grade(compute_metrics([100.0, 100.0])) == "INSUFFICIENT"


def test_returns_basis_scales_sharpe_not_pnl():
    pnls = [100.0, -50.0, 80.0, -30.0]
    big = compute_metrics(pnls, returns_basis=1_000_000.0)
    small = compute_metrics(pnls, returns_basis=10_000.0)
    # P&L stats identical regardless of basis
    assert big["total_pnl"] == small["total_pnl"]
    assert big["profit_factor"] == small["profit_factor"]
    # Sharpe is scale-invariant to the basis (ratio of mean/std), so equal
    assert abs(big["sharpe"] - small["sharpe"]) < 1e-9


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
            passed += 1
        except Exception:
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"{passed}/{len(fns)} passed")
