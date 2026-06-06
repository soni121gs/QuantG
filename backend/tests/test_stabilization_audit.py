from unittest.mock import AsyncMock, MagicMock, patch
import os
import sys
from datetime import datetime, timezone, timedelta
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

@pytest.mark.anyio
async def test_paper_mode_never_triggers_live_broker_placements():
    from server import _place_upstox_order
    
    mock_db = MagicMock()
    # Mock disarmed live arm state
    mock_db.live_arm_state.find_one = AsyncMock(return_value={"user_id": "user-1", "armed": False})
    
    with patch("server.db", mock_db), \
         patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "false"}):
        with pytest.raises(RuntimeError) as exc:
            await _place_upstox_order(
                user_id="user-1",
                instrument_token="NSE_EQ|INE002A01018",
                side="BUY",
                quantity=10,
                order_type="MARKET",
                product="MIS"
            )
        assert "CORE_ENGINE_LIVE_ENABLED is set to false" in str(exc.value)

@pytest.mark.anyio
async def test_profile_live_setup_mode_allows_pre_market_upstox_connection_later():
    from server import ProfileUpdateReq, update_profile

    mock_db = MagicMock()
    mock_db.users.update_one = AsyncMock()

    async def fake_get_profile(user):
        return {
            "id": user["id"],
            "email": user["email"],
            "paper_mode": False,
            "allow_simulated_prices": False,
        }

    user = {"id": "user-1", "email": "trader@quantg.com", "created_at": "now"}

    with patch("server.db", mock_db), \
         patch("server.get_profile", fake_get_profile), \
         patch("server._sync_strategy_modes_to_profile", new_callable=AsyncMock) as sync_modes, \
         patch("server.get_user_upstox_status", new_callable=AsyncMock, return_value={"token_valid": False}), \
         patch("server._is_nse_market_open", return_value=False), \
         patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "false"}):
        result = await update_profile(ProfileUpdateReq(paper_mode=False), user=user)

    mock_db.users.update_one.assert_awaited_once_with(
        {"id": "user-1"},
        {"$set": {"paper_mode": False, "allow_simulated_prices": False}},
    )
    sync_modes.assert_awaited_once_with("user-1", False)
    assert result["paper_mode"] is False
    assert result["allow_simulated_prices"] is False

@pytest.mark.anyio
async def test_stale_quote_price_yields_skips():
    from server import _price_integrity_guard
    
    instr = MagicMock()
    instr.exchange = "NFO"
    instr.tradingsymbol = "NIFTY26060524900CE"
    instr.instrument_token = "NSE_FO|12345"
    instr.asset_class = "OPTION_LONG"
    
    intent = MagicMock()
    intent.instrument = instr
    intent.intent = "OPEN_LONG"
    
    # 70 seconds old quote
    stale_time = (datetime.now(timezone.utc) - timedelta(seconds=70)).isoformat()
    
    result = _price_integrity_guard(
        paper=True,
        intent=intent,
        option_contract={"instrument_key": "NSE_FO|12345", "option_type": "CE", "strike": 24900},
        market_snapshot={
            "ltp": 34.25,
            "source": "upstox-rest-quote",
            "feed": "upstox-rest-quote",
            "received_at": stale_time,
            "instrument_key": "NSE_FO|12345",
        },
        market_session={"open": True, "segment": "NSE_FO"},
    )
    
    assert not result["ok"]
    assert result["reason_code"] == "SKIPPED_QUOTE_STALE"
    assert "quote is stale" in result["human_reason"]

@pytest.mark.anyio
async def test_missing_quote_timestamp_yields_skips():
    from server import _price_integrity_guard
    
    instr = MagicMock()
    instr.exchange = "NFO"
    instr.tradingsymbol = "NIFTY26060524900CE"
    instr.instrument_token = "NSE_FO|12345"
    instr.asset_class = "OPTION_LONG"
    
    intent = MagicMock()
    intent.instrument = instr
    intent.intent = "OPEN_LONG"
    
    result = _price_integrity_guard(
        paper=True,
        intent=intent,
        option_contract={"instrument_key": "NSE_FO|12345", "option_type": "CE", "strike": 24900},
        market_snapshot={
            "ltp": 34.25,
            "source": "upstox-rest-quote",
            "feed": "upstox-rest-quote",
            "received_at": None,
            "instrument_key": "NSE_FO|12345",
        },
        market_session={"open": True, "segment": "NSE_FO"},
    )
    
    assert not result["ok"]
    assert result["reason_code"] == "SKIPPED_QUOTE_STALE"
    assert "quote timestamp is missing" in result["human_reason"]

@pytest.mark.anyio
async def test_version_endpoint_metadata():
    import server
    from fastapi.testclient import TestClient
    
    client = TestClient(server.app)
    
    with patch("server.get_git_info", return_value=("commit123", "main", False)):
        response = client.get("/api/version")
        
    assert response.status_code == 200
    data = response.json()
    assert "backend_version" in data
    assert data["git_commit"] == "commit123"
    assert data["git_branch"] == "main"
    assert "up_time_seconds" in data

@pytest.mark.anyio
async def test_live_readiness_checklist():
    import server
    from fastapi.testclient import TestClient
    
    client = TestClient(server.app)
    
    mock_db = MagicMock()
    # Return disarmed states, configurations
    mock_db.live_arm_state.find_one = AsyncMock(return_value={"armed": False, "global_live_enabled": False})
    mock_db.system_config.find_one = AsyncMock(return_value={"lot_sizes": {"NIFTY": 50}})
    mock_db.funds.find_one = AsyncMock(return_value={"available_margin": 100000.0})
    mock_db.risk_state.find_one = AsyncMock(return_value={"active": False})
    mock_db.orders.count_documents = AsyncMock(return_value=0)
    
    # Mock user auth dependency
    async def fake_get_current_user():
        return {"id": "user-1", "email": "trader@quantg.com"}
        
    server.app.dependency_overrides[server.get_current_user] = fake_get_current_user
    
    with patch("server.db", mock_db), \
         patch("server.get_user_upstox_status", new_callable=AsyncMock, return_value={"keys_saved": True, "connected": True, "token_valid": True}):
        response = client.get("/api/trading/live-readiness")
        
    server.app.dependency_overrides.clear()
    
    assert response.status_code == 200
    data = response.json()
    assert "checks" in data
    assert "ok" in data
    
    checks = {c["id"]: c for c in data["checks"]}
    assert "live_env_enabled" in checks
    assert "live_db_armed" in checks
    assert "upstox_keys" in checks
    assert "upstox_token" in checks
    assert "feed_status" in checks
    assert "exchange_rules" in checks
    assert "funds_presence" in checks
    assert "kill_switch" in checks
    assert "stale_reconciliation" in checks
