from core.portfolio_narrator import explain_portfolio


def test_narrator_only_reports_snapshot_evidence_and_missing_data():
    text = explain_portfolio({
        "open_positions": 2, "defined_risk": 1200, "realized_pnl": -50,
        "unrealized_pnl": 100, "total_pnl": 50,
        "by_underlying": [{"name": "NIFTY", "risk": 1200, "positions": 2}],
        "data_quality": {"missing_greeks": ["vega"]},
    }, {"estimated_pnl": -300, "scenario": {"underlying_move_pct": -0.02, "iv_change_points": 5, "holding_days": 1}})

    assert "NIFTY" in text
    assert "₹1,200.00" in text
    assert "vega" in text
    assert "₹-300.00" in text
    assert "not an order recommendation" in text
