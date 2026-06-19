import pytest
import sys
import os
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# Ensure backend and hermes are in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "hermes"))

# Set environment variables so hermes initializes without errors
os.environ["TELEGRAM_BOT_TOKEN"] = "mock_bot_token"
os.environ["TELEGRAM_CHAT_ID"] = "mock_chat_id"

import agent

def test_is_market_hours():
    # Mocking Monday 10:00 IST = Monday 04:30 UTC
    with patch("agent.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 6, 22, 4, 30, tzinfo=timezone.utc)
        assert agent.is_market_hours() is True

    # Mocking Saturday 10:00 IST = Saturday 04:30 UTC
    with patch("agent.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 6, 20, 4, 30, tzinfo=timezone.utc)
        assert agent.is_market_hours() is False

    # Mocking Monday 08:00 IST = Monday 02:30 UTC
    with patch("agent.datetime") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 6, 22, 2, 30, tzinfo=timezone.utc)
        assert agent.is_market_hours() is False


@patch("agent.requests.post")
def test_client_login(mock_post):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "mocked_jwt_token"}
    mock_post.return_value = mock_response

    client = agent.QuantGClient("http://localhost:8000/api", "demo", "pass")
    success = client.login()
    
    assert success is True
    assert client.token == "mocked_jwt_token"
    mock_post.assert_called_once_with(
        "http://localhost:8000/api/auth/login",
        json={"email": "demo", "password": "pass"},
        timeout=10
    )


@patch("agent.requests.request")
def test_client_request_with_token(mock_req):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"status": "ok"}
    mock_req.return_value = mock_response

    client = agent.QuantGClient("http://localhost:8000/api", "demo", "pass")
    client.token = "active_token"

    res = client.request("GET", "/some/path")
    assert res is not None
    assert res.json() == {"status": "ok"}
    
    headers_called = mock_req.call_args[1]["headers"]
    assert headers_called["Authorization"] == "Bearer active_token"


@patch("agent.send_telegram_alert")
def test_run_premarket_check(mock_send):
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "ok": True,
        "current_mode": "PAPER",
        "checks": [
            {"id": "broker_keys", "ok": True, "label": "Upstox credentials saved"},
            {"id": "upstox_session", "ok": False, "label": "Active Upstox session", "hint": "Reconnect Upstox"}
        ]
    }
    mock_client.request.return_value = mock_resp

    with patch("agent.client", mock_client):
        agent.run_premarket_check("2026-06-22")
        
    mock_send.assert_called_once()
    msg_text = mock_send.call_args[0][0]
    assert "Hermes Pre-Market Readiness Report" in msg_text
    assert "2026-06-22" in msg_text
    assert "PAPER" in msg_text
    assert "Active Upstox session" in msg_text
    assert "Reconnect Upstox" in msg_text


@patch("agent.send_telegram_alert")
def test_run_eod_report(mock_send):
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "generated_at": "2026-06-22T15:35:00",
        "total_realized_pnl": 5500.0,
        "total_unrealized_pnl": -500.0,
        "trades_taken": 3,
        "signals_fired": 10,
        "signals_filtered": 7,
        "market_regime": "TRENDING",
        "best_strategy": {"name": "SMA Scalper", "pnl": 6000.0},
        "worst_strategy": {"name": "EMA Fader", "pnl": -500.0},
        "strategies": [
            {"name": "SMA Scalper", "pnl": 6000.0, "trade_count": 2},
            {"name": "EMA Fader", "pnl": -500.0, "trade_count": 1}
        ]
    }
    mock_client.request.return_value = mock_resp

    with patch("agent.client", mock_client):
        agent.run_eod_report("2026-06-22")

    mock_send.assert_called_once()
    msg_text = mock_send.call_args[0][0]
    assert "Hermes EOD Trading Report" in msg_text
    assert "TRENDING" in msg_text
    assert "Rs 5,000.00" in msg_text
    assert "SMA Scalper" in msg_text
    assert "EMA Fader" in msg_text
