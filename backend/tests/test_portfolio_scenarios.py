from core.portfolio_scenarios import build_portfolio_scenario, build_portfolio_scenario_grid


def test_scenario_computes_explicit_greek_estimate_and_reports_missing_inputs():
    result = build_portfolio_scenario([
        {"id": "p1", "open_quantity": 2, "scenario_spot": 100, "greeks": {"delta": 1, "gamma": 0, "theta": -2, "vega": 3}},
        {"id": "p2", "scenario_spot": 100, "greeks": {"delta": 1}},
    ], move_pct=0.1, iv_points=2, days=1)

    assert result["read_only"] is True
    assert result["status"] == "PARTIAL"
    assert result["estimated_pnl"] == 28.0
    assert result["coverage"] == {"computed": 1, "total": 2, "missing_inputs": 1}
    assert result["positions"][1]["status"] == "NOT_COMPUTABLE"


def test_scenario_grid_reports_worst_and_best_without_side_effects():
    positions = [{"id": "p1", "scenario_spot": 100, "quantity": 1,
                  "greeks": {"delta": 1, "gamma": 0, "theta": 0, "vega": 1}}]
    result = build_portfolio_scenario_grid(positions, moves=[-0.1, 0.1], iv_changes=[-5, 5], days=1)
    assert result["read_only"] is True
    assert len(result["cells"]) == 4
    assert result["coverage"] == {"computed": 4, "total": 4}
    assert result["worst_case"]["estimated_pnl"] < result["best_case"]["estimated_pnl"]
    assert result["warning"].startswith("Stress surface only")
