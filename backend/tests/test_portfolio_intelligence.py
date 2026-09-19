from datetime import datetime, timezone

from core.portfolio_intelligence import build_portfolio_snapshot


def test_snapshot_is_read_only_and_aggregates_risk_pnl_and_greeks():
    result = build_portfolio_snapshot([
        {"status": "OPEN", "underlying": "NIFTY", "strategy_id": "s1", "max_loss_total": 1000, "updated_at": "2026-01-01T00:00:00Z",
         "pnl": 125, "greeks": {"delta": -0.2, "gamma": 0.01, "theta": 4, "vega": -2}},
        {"status": "CLOSED", "underlying": "NIFTY", "strategy_id": "s1", "max_loss_total": 500, "pnl": 80},
    ], [{"realized_pnl": -50, "strategy_id": "s1", "underlying": "NIFTY", "created_at": "2026-01-02T12:00:00Z"}], now=datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert result["read_only"] is True
    assert result["open_positions"] == 1
    assert result["defined_risk"] == 1000.0
    assert result["risk_budget"]["heat_budget"] > 0
    assert result["risk_budget"]["heat_utilization"] > 0
    assert result["realized_pnl"] == -50.0
    assert result["daily_realized_pnl"] == -50.0
    assert result["unrealized_pnl"] == 125.0
    assert result["greeks"]["delta"]["value"] == -0.2
    assert result["data_quality"]["missing_greeks"] == []
    assert [alert["code"] for alert in result["risk_alerts"]] == ["UNDERLYING_CONCENTRATION"]
    assert result["realized_by_strategy"] == [{"name": "S1", "fills": 1, "realized_pnl": -50.0}]
    assert result["data_quality"]["freshness"]["positions"]["status"] == "AVAILABLE"
    assert result["data_quality"]["freshness"]["fills"]["status"] == "AVAILABLE"
