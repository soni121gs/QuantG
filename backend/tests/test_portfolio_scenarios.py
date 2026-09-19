from core.portfolio_scenarios import build_portfolio_scenario


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
