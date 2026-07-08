import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.quant_research_metrics import (
    bayesian_win_rate,
    bootstrap_expectancy_ci,
    fractional_kelly,
    max_drawdown,
    profit_factor,
    summarize_research_metrics,
)


def test_basic_metric_components():
    pnls = [100, -40, 120, -20]
    assert max_drawdown(pnls) == 40
    assert round(profit_factor(pnls), 2) == 3.67
    assert 0 < fractional_kelly(0.6, 100, 50) < 1
    assert round(bayesian_win_rate(8, 2), 3) == 0.75
    ci = bootstrap_expectancy_ci(pnls, iterations=50)
    assert ci["low"] < ci["high"]


def test_robust_edge_candidate():
    pnls = [240, 220, 260, 180, 300, -120] * 8
    result = summarize_research_metrics(
        pnls,
        per_trade_costs=(0, 25),
        trials_tested=1,
        min_sample=30,
    )

    assert result["sample_n"] == 48
    assert result["expectancy"] > 0
    assert result["profit_factor"] > 1
    assert result["verdict"] == "CANDIDATE_EDGE"
    assert result["fractional_kelly"] > 0


def test_fragile_edge_fails_cost_stress():
    pnls = [60, 55, 50, -40, 45, -30] * 6
    result = summarize_research_metrics(
        pnls,
        per_trade_costs=(0, 50),
        trials_tested=1,
        min_sample=30,
    )

    assert result["sample_n"] == 36
    assert result["expectancy"] > 0
    assert result["slippage_stress"]["50.0"]["passes"] is False
    assert result["verdict"] == "FRAGILE"


def test_overfit_winner_gets_penalized():
    pnls = [100, 90, 80, -70, 110, -60] * 6
    result = summarize_research_metrics(
        pnls,
        per_trade_costs=(0, 25),
        trials_tested=100,
        min_sample=30,
    )

    assert result["multiple_testing_penalty"] > 0
    assert any("multiple-testing" in warning for warning in result["warnings"])
    assert result["verdict"] in {"FRAGILE", "NO_EDGE_NEGATIVE"}


def test_high_win_rate_can_still_be_negative_expectancy():
    pnls = [20] * 32 + [-500] * 8
    result = summarize_research_metrics(
        pnls,
        per_trade_costs=(0,),
        trials_tested=1,
        min_sample=30,
    )

    assert result["win_rate"] == 0.8
    assert result["expectancy"] < 0
    assert result["verdict"] == "NO_EDGE_NEGATIVE"
    assert any("high win-rate" in warning for warning in result["warnings"])
