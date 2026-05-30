from datetime import datetime, timezone
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brokers.upstox_gateway import UpstoxGateway
from execution_state import ExecutionStateManager


def test_upstox_gateway_parse_quote_ltp_unmatched_returns_none():
    payload = {
        "status": "success",
        "data": {
            "NSE_EQ|INE002A01018": {
                "last_price": 2945.50,
                "ltp": 2945.50
            },
            "NSE_EQ|INE467B01029": {
                "last_price": 4120.20,
                "ltp": 4120.20
            }
        }
    }
    
    # Matching exact key should succeed
    price = UpstoxGateway.parse_quote_ltp(payload, "NSE_EQ|INE002A01018")
    assert price == 2945.50
    
    # Non-matching key should return None and NOT fall through to returning first element!
    price_unmatched = UpstoxGateway.parse_quote_ltp(payload, "NSE_EQ|INE000000000")
    assert price_unmatched is None
    
    # If no instrument_key is provided, fallback to first one is acceptable
    price_fallback = UpstoxGateway.parse_quote_ltp(payload, None)
    assert price_fallback == 2945.50 or price_fallback == 4120.20


def test_upstox_gateway_handle_mock_request_returns_unique_equity_prices():
    gateway = UpstoxGateway()
    gateway.access_token = "mock_live_upstox_token"
    
    # Requesting RELIANCE and TCS quotes in mock mode
    payload = gateway.search_instruments("NIFTY")  # Dummy call or just call _request directly
    result = gateway._request("GET", "/v2/market-quote/ltp", params={"instrument_key": "NSE_EQ|INE002A01018,NSE_EQ|INE467B01029"})
    
    assert result["status"] == "success"
    data = result["data"]
    
    reliance_quote = data.get("NSE_EQ|INE002A01018")
    tcs_quote = data.get("NSE_EQ|INE467B01029")
    
    assert reliance_quote is not None
    assert tcs_quote is not None
    
    # Ensure they are not fabricated identical prices (e.g. they aren't both 34.50)
    assert reliance_quote["last_price"] != tcs_quote["last_price"]
    assert abs(reliance_quote["last_price"] - 2945.50) < 50.0  # Drift is within reasonable bounds
    assert abs(tcs_quote["last_price"] - 4120.20) < 80.0


def test_merge_position_risk_matches_by_strategy_id_and_labels_orphans():
    manager = ExecutionStateManager()
    
    # Broker positions: 2 positions (one has strategy_id, one is orphan/stale)
    broker_positions = [
        {
            "symbol": "TCS",
            "qty": 10,
            "avg_price": 4100.0,
            "ltp": 4120.0,
            "pnl": 200.0,
            "strategy_id": "strat_active_123",
            "mode": "paper",
        },
        {
            "symbol": "INFY",
            "qty": 20,
            "avg_price": 1880.0,
            "ltp": 1890.0,
            "pnl": 200.0,
            "strategy_id": "strat_orphan_999",
            "mode": "paper",
        }
    ]
    
    # Strategy positions (active strategy rows in db.strategy_positions)
    # Only strat_active_123 is running
    strategy_rows = [
        {
            "id": "strategy_pos_active_row",
            "strategy_id": "strat_active_123",
            "strategy_name": "Active TCS Strategy",
            "symbol": "TCS",
            "qty": 10,
            "avg_price": 4100.0,
            "status": "OPEN",
            "mode": "paper",
        }
    ]
    
    ledger_risk = {
        "strat_active_123": {
            "stop_loss": 3950.0,
            "take_profit": 4300.0,
            "trailing_sl": None,
            "position_status": "ACTIVE",
        }
    }
    
    merged = manager._merge_position_risk(broker_positions, strategy_rows, ledger_risk)
    
    assert len(merged) == 2
    
    # Find TCS merged position
    tcs = next(p for p in merged if p["symbol"] == "TCS")
    assert tcs["strategy_id"] == "strat_active_123"
    assert tcs["strategy_name"] == "Active TCS Strategy"
    assert tcs["stop_loss"] == 3950.0
    assert tcs["take_profit"] == 4300.0
    assert tcs["execution_status"] == "OPEN"
    
    # Find INFY merged position (orphan)
    infy = next(p for p in merged if p["symbol"] == "INFY")
    assert infy["strategy_id"] == "strat_orphan_999"
    assert "Orphan" in infy["strategy_name"]
    assert infy["stop_loss"] is None
    assert infy["take_profit"] is None
    assert infy["execution_status"] == "ORPHAN"
    assert infy["ledger_status"] == "ORPHAN"
