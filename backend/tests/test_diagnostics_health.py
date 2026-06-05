import sys
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Mock server.db and get_user_upstox_status prior to importing server to bypass connection/env checks
with patch("server.db", MagicMock()) as mock_db, \
     patch("server.get_user_upstox_status", AsyncMock()) as mock_upstox:
    from server import diagnostics_health


@pytest.mark.anyio
async def test_diagnostics_health_endpoint():
    mock_user = {"id": "test_user_id"}
    
    # 1. Setup mock strategies list
    mock_db.strategies.find.return_value.to_list = AsyncMock(return_value=[
        {"id": "strat_1", "name": "Strategy 1", "status": "live", "mode": "live", "symbol": "SENSEX", "broker": "upstox"}
    ])
    
    # 2. Setup mock position locks
    mock_db.strategy_position_locks.find.return_value.to_list = AsyncMock(return_value=[
        {"strategy_id": "strat_1", "trading_symbol": "SENSEX", "instrument_key": "BSE_FO|12345", "created_at": "2026-05-27T18:00:00Z", "expires_at": "2026-05-27T18:30:00Z"}
    ])
    
    # 3. Setup mock rejected/cancelled orders list
    mock_db.orders.find.return_value.sort.return_value.limit.return_value.to_list = AsyncMock(return_value=[
        {"id": "order_1", "symbol": "SENSEX", "side": "BUY", "qty": 1, "status": "REJECTED", "status_message": "Insufficient funds", "created_at": "2026-05-27T18:10:00Z"}
    ])
    
    # 4. Setup mock Upstox gateway status
    mock_upstox.return_value = {
        "connected": True,
        "gateway": {
            "connected": True,
            "ticks": 42,
            "last_tick_at": "2026-05-27T18:25:00Z",
            "last_error": None
        }
    }
    
    with patch("server.db", mock_db), patch("server.get_user_upstox_status", mock_upstox):
        res = await diagnostics_health(user=mock_user)
        
    assert res["status"] == "healthy"
    assert len(res["strategies"]) == 1
    assert res["strategies"][0]["id"] == "strat_1"
    assert res["feed"]["connected"] is True
    assert res["feed"]["ticks_received"] == 42
    assert len(res["position_locks"]) == 1
    assert res["position_locks"][0]["strategy_id"] == "strat_1"
    assert len(res["recent_failed_orders"]) == 1
    assert res["recent_failed_orders"][0]["status_message"] == "Insufficient funds"
