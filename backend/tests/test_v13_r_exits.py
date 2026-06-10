import sys
import os
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from core import db, get_current_user

# Mock user for FastAPI requests
async def fake_get_current_user():
    return {"id": "user-1", "email": "trader@quantg.com", "role": "owner"}

@pytest.fixture
def test_client():
    from server import app
    app.dependency_overrides[get_current_user] = fake_get_current_user
    yield TestClient(app)
    app.dependency_overrides.clear()

# T1: R stop/target calculation (e.g. entry 100, risk_pct 8, target_R 2 gives SL 92, TP 116)
@pytest.mark.anyio
async def test_r_bracket_calculation():
    mock_db = MagicMock()
    mock_db.strategy_positions.update_one = AsyncMock()
    
    reservation = {
        "id": "res-123",
        "user_id": "user-1",
        "position_side": "LONG",
        "exchange": "NFO",
        "trading_symbol": "NIFTY26CE",
        "tp_sl_tsl_config": {
            "stop_loss_pct": 8.0,
            "take_profit_pct": 12.0
        },
        "initial_stop_R": 1.0,
        "target_R": 2.0,
        "trail_after_R": 1.5,
    }
    
    with patch("server.db", mock_db):
        await server._activate_strategy_position(
            reservation,
            order_id="order-123",
            broker_order_id="broker-123",
            average_buy_price=100.0,
            quantity=50,
            paper=True
        )
        
    assert mock_db.strategy_positions.update_one.call_count == 1
    update_payload = mock_db.strategy_positions.update_one.call_args[0][1]["$set"]
    
    assert update_payload["r_initial_risk_amount"] == 8.0
    assert update_payload["r_stop_loss_price"] == 92.0
    assert update_payload["r_take_profit_price"] == 116.0
    assert update_payload["r_entry_price"] == 100.0
    assert update_payload["r_current_R"] == 0.0
    assert update_payload["r_max_R_seen"] == 0.0
    assert update_payload["r_trailing_active"] is False
    assert update_payload["r_trailing_stop_price"] == 92.0
    assert update_payload["best_price_seen"] == 100.0

# T2: Missing target_R fallback does not crash and uses fallbacks
@pytest.mark.anyio
async def test_r_bracket_missing_fields_fallback():
    mock_db = MagicMock()
    mock_db.strategy_positions.update_one = AsyncMock()
    
    reservation = {
        "id": "res-123",
        "user_id": "user-1",
        "position_side": "LONG",
        "exchange": "NFO",
        "trading_symbol": "NIFTY26CE",
        "tp_sl_tsl_config": {},
    }
    
    with patch("server.db", mock_db):
        await server._activate_strategy_position(
            reservation,
            order_id="order-123",
            broker_order_id="broker-123",
            average_buy_price=100.0,
            quantity=50,
            paper=True
        )
        
    update_payload = mock_db.strategy_positions.update_one.call_args[0][1]["$set"]
    assert update_payload["r_initial_risk_amount"] == 8.0
    assert update_payload["r_stop_loss_price"] == 92.0
    assert update_payload["r_take_profit_price"] == 116.0

# T3: Default catalog upgrade removes all signal_only templates
def test_exit_mode_never_signal_only():
    for template in server.DEFAULT_OPTION_STRATEGIES:
        exit_mode = template.get("risk", {}).get("exit_mode")
        assert exit_mode != "signal_only", f"Strategy template {template.get('name')} must not be signal_only"

# T4: V13 metadata propagates successfully from signal to order intent and strategy position
@pytest.mark.anyio
async def test_metadata_propagation():
    mock_db = MagicMock()
    signal_doc = {
        "id": "sig-123",
        "setup_type": "breakout",
        "confidence": 92.0,
        "entry_reason": "RSI crossing 70",
        "target_R": 3.0,
        "initial_stop_R": 1.2,
        "trail_after_R": 2.0,
        "max_hold_minutes": 45,
        "invalidation_rule": "time_only",
        "regime_required": "bullish",
        "option_selection_preference": "OTM",
        "signal_version": "v13",
        "strategy_logic_version": "1.1",
        "default_strategy_version": "v13-live-brain-r1",
    }
    mock_db.signals.find_one = AsyncMock(return_value=signal_doc)
    mock_db.strategy_positions.insert_one = AsyncMock()
    mock_db.strategy_positions.find_one = AsyncMock(return_value=None)
    mock_db.strategy_position_locks.insert_one = AsyncMock()
    mock_db.orders.insert_one = AsyncMock()
    mock_db.orders.find_one = AsyncMock(return_value=None)
    mock_db.strategies.find_one = AsyncMock(return_value={"id": "strat-123"})
    
    with patch("server.db", mock_db):
        res_doc = await server._reserve_strategy_position(
            user_id="user-1",
            strategy_id="strat-123",
            instrument_key="NSE_FO|54321",
            trading_symbol="NIFTY26CE",
            exchange="NFO",
            instrument_token="54321",
            quantity=50,
            entry_price=100.0,
            position_side="LONG",
            source="runner",
            signal_id="sig-123",
        )
        
    assert res_doc["confidence"] == 92.0
    assert res_doc["target_R"] == 3.0
    assert res_doc["initial_stop_R"] == 1.2
    assert res_doc["r_metadata_source"] == "v13_signal"

# T5: Paper positions exit properly when target_R is hit
@pytest.mark.anyio
async def test_paper_exit_target_hit():
    mock_db = MagicMock()
    pos = {
        "id": "pos-123",
        "user_id": "user-1",
        "trading_symbol": "NIFTY26CE",
        "exchange": "NFO",
        "position_side": "LONG",
        "r_entry_price": 100.0,
        "r_initial_risk_amount": 8.0,
        "r_take_profit_price": 116.0,
        "r_stop_loss_price": 92.0,
        "r_max_R_seen": 0.0,
        "r_trailing_active": False,
        "r_trailing_stop_price": 92.0,
        "best_price_seen": 100.0,
        "target_R": 2.0,
        "trail_after_R": 1.5,
    }
    
    find_mock = MagicMock()
    find_mock.to_list = AsyncMock(return_value=[pos])
    mock_db.strategy_positions.find = MagicMock(return_value=find_mock)
    mock_db.strategy_positions.update_one = AsyncMock()
    
    with patch("server.db", mock_db), patch("server._current_ltp_for_symbol", AsyncMock(return_value=110.0)):
        exit_reason = await server._evaluate_strategy_risk("user-1", "strat-123")
        assert exit_reason is None
        assert mock_db.strategy_positions.update_one.call_count == 1
        update_set = mock_db.strategy_positions.update_one.call_args[0][1]["$set"]
        assert update_set["r_current_R"] == 1.25
        assert update_set["best_price_seen"] == 110.0
        assert update_set["r_max_R_seen"] == 1.25
        
    mock_db.strategy_positions.update_one.reset_mock()
    
    with patch("server.db", mock_db), patch("server._current_ltp_for_symbol", AsyncMock(return_value=117.0)):
        exit_reason = await server._evaluate_strategy_risk("user-1", "strat-123")
        assert exit_reason == "R_TARGET_HIT"

# T6: Paper positions exit properly when stop_loss_price is hit
@pytest.mark.anyio
async def test_paper_exit_stop_loss_hit():
    mock_db = MagicMock()
    pos = {
        "id": "pos-123",
        "user_id": "user-1",
        "trading_symbol": "NIFTY26CE",
        "exchange": "NFO",
        "position_side": "LONG",
        "r_entry_price": 100.0,
        "r_initial_risk_amount": 8.0,
        "r_take_profit_price": 116.0,
        "r_stop_loss_price": 92.0,
        "r_max_R_seen": 0.0,
        "r_trailing_active": False,
        "r_trailing_stop_price": 92.0,
        "best_price_seen": 100.0,
        "target_R": 2.0,
        "trail_after_R": 1.5,
    }
    
    find_mock = MagicMock()
    find_mock.to_list = AsyncMock(return_value=[pos])
    mock_db.strategy_positions.find = MagicMock(return_value=find_mock)
    
    with patch("server.db", mock_db), patch("server._current_ltp_for_symbol", AsyncMock(return_value=91.0)):
        exit_reason = await server._evaluate_strategy_risk("user-1", "strat-123")
        assert exit_reason == "R_STOP_LOSS_HIT"

# T7: Trailing stop activates after trail_after_R, moves above breakeven after +1R, and never moves downward
@pytest.mark.anyio
async def test_trailing_stop_dynamics():
    mock_db = MagicMock()
    pos = {
        "id": "pos-123",
        "user_id": "user-1",
        "trading_symbol": "NIFTY26CE",
        "exchange": "NFO",
        "position_side": "LONG",
        "r_entry_price": 100.0,
        "r_initial_risk_amount": 8.0,
        "r_take_profit_price": 124.0,
        "r_stop_loss_price": 92.0,
        "r_max_R_seen": 0.0,
        "r_trailing_active": False,
        "r_trailing_stop_price": 92.0,
        "best_price_seen": 100.0,
        "target_R": 3.0,
        "trail_after_R": 1.5,
    }
    
    find_mock = MagicMock()
    find_mock.to_list = AsyncMock(return_value=[pos])
    mock_db.strategy_positions.find = MagicMock(return_value=find_mock)
    mock_db.strategy_positions.update_one = AsyncMock()
    
    with patch("server.db", mock_db), patch("server._current_ltp_for_symbol", AsyncMock(return_value=108.0)):
        exit_reason = await server._evaluate_strategy_risk("user-1", "strat-123")
        assert exit_reason is None
        update_set = mock_db.strategy_positions.update_one.call_args[0][1]["$set"]
        assert update_set["r_trailing_active"] is False
        assert update_set["r_trailing_stop_price"] == 92.0
        
    pos.update(update_set)
    mock_db.strategy_positions.update_one.reset_mock()
    
    with patch("server.db", mock_db), patch("server._current_ltp_for_symbol", AsyncMock(return_value=113.0)):
        exit_reason = await server._evaluate_strategy_risk("user-1", "strat-123")
        assert exit_reason is None
        update_set = mock_db.strategy_positions.update_one.call_args[0][1]["$set"]
        assert update_set["r_trailing_active"] is True
        assert update_set["r_trailing_stop_price"] == 107.4
        
    pos.update(update_set)
    mock_db.strategy_positions.update_one.reset_mock()
    
    with patch("server.db", mock_db), patch("server._current_ltp_for_symbol", AsyncMock(return_value=109.0)):
        exit_reason = await server._evaluate_strategy_risk("user-1", "strat-123")
        assert exit_reason is None
        update_set = mock_db.strategy_positions.update_one.call_args[0][1]["$set"]
        assert update_set["r_trailing_stop_price"] == 107.4

# T8: Time exit triggers after max_hold_minutes expires
@pytest.mark.anyio
async def test_time_exit_triggered():
    mock_db = MagicMock()
    entry_time = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
    pos = {
        "id": "pos-123",
        "user_id": "user-1",
        "trading_symbol": "NIFTY26CE",
        "exchange": "NFO",
        "position_side": "LONG",
        "r_entry_price": 100.0,
        "r_initial_risk_amount": 8.0,
        "r_take_profit_price": 116.0,
        "r_stop_loss_price": 92.0,
        "r_max_R_seen": 0.0,
        "r_trailing_active": False,
        "r_trailing_stop_price": 92.0,
        "best_price_seen": 100.0,
        "target_R": 2.0,
        "trail_after_R": 1.5,
        "max_hold_minutes": 60,
        "entry_time": entry_time
    }
    
    find_mock = MagicMock()
    find_mock.to_list = AsyncMock(return_value=[pos])
    mock_db.strategy_positions.find = MagicMock(return_value=find_mock)
    
    with patch("server.db", mock_db), patch("server._current_ltp_for_symbol", AsyncMock(return_value=105.0)):
        exit_reason = await server._evaluate_strategy_risk("user-1", "strat-123")
        assert exit_reason == "R_TIME_EXIT"

# T9: Stale/missing live LTP does not trigger false exit (keeps under review)
@pytest.mark.anyio
async def test_stale_ltp_under_review():
    mock_db = MagicMock()
    pos = {
        "id": "pos-123",
        "user_id": "user-1",
        "trading_symbol": "NIFTY26CE",
        "exchange": "NFO",
        "position_side": "LONG",
        "r_entry_price": 100.0,
        "r_initial_risk_amount": 8.0,
        "r_take_profit_price": 116.0,
        "r_stop_loss_price": 92.0,
        "r_max_R_seen": 0.0,
        "r_trailing_active": False,
        "r_trailing_stop_price": 92.0,
        "best_price_seen": 100.0,
        "target_R": 2.0,
        "trail_after_R": 1.5,
    }
    
    find_mock = MagicMock()
    find_mock.to_list = AsyncMock(return_value=[pos])
    mock_db.strategy_positions.find = MagicMock(return_value=find_mock)
    
    with patch("server.db", mock_db), patch("server._current_ltp_for_symbol", AsyncMock(return_value=None)):
        exit_reason = await server._evaluate_strategy_risk("user-1", "strat-123")
        assert exit_reason is None

# T10: /api/ops/r-exit-status successfully returns running active position metrics
def test_r_exit_status_endpoint(test_client):
    mock_db = MagicMock()
    pos = {
        "id": "pos-123",
        "user_id": "user-1",
        "strategy_id": "strat-123",
        "trading_symbol": "NIFTY26CE",
        "exchange": "NFO",
        "position_side": "LONG",
        "status": "OPEN",
        "average_buy_price": 100.0,
        "r_entry_price": 100.0,
        "r_initial_risk_amount": 8.0,
        "r_take_profit_price": 116.0,
        "r_stop_loss_price": 92.0,
        "r_max_R_seen": 0.5,
        "r_trailing_active": False,
        "r_trailing_stop_price": 92.0,
        "best_price_seen": 104.0,
        "target_R": 2.0,
        "trail_after_R": 1.5,
        "max_hold_minutes": 60,
        "entry_time": datetime.now(timezone.utc).isoformat()
    }
    
    find_mock = MagicMock()
    find_mock.to_list = AsyncMock(return_value=[pos])
    mock_db.strategy_positions.find = MagicMock(return_value=find_mock)
    mock_db.strategies.find_one = AsyncMock(return_value={"id": "strat-123", "name": "VWAP Trend strategy"})
    
    with patch("routes.ops.db", mock_db), patch("server._current_ltp_for_symbol", AsyncMock(return_value=104.0)):
        response = test_client.get("/api/ops/r-exit-status")
        
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert len(data["positions"]) == 1
    assert data["positions"][0]["strategy_name"] == "VWAP Trend strategy"
    assert data["positions"][0]["ltp"] == 104.0
    assert data["positions"][0]["r_max_R_seen"] == 0.5

# T11: Backward-compatible recovery: older DB positions without V13 metadata resolve fallback calculations gracefully
@pytest.mark.anyio
async def test_backward_compatible_recovery():
    mock_db = MagicMock()
    pos = {
        "id": "pos-123",
        "user_id": "user-1",
        "trading_symbol": "NIFTY26CE",
        "exchange": "NFO",
        "position_side": "LONG",
        "average_buy_price": 100.0,
        "tp_sl_tsl_config": {
            "stop_loss_pct": 10.0,
        },
        "initial_stop_R": 1.5,
        "target_R": 2.5,
    }
    
    find_mock = MagicMock()
    find_mock.to_list = AsyncMock(return_value=[pos])
    mock_db.strategy_positions.find = MagicMock(return_value=find_mock)
    mock_db.strategy_positions.update_one = AsyncMock()
    
    with patch("server.db", mock_db), patch("server._current_ltp_for_symbol", AsyncMock(return_value=105.0)):
        exit_reason = await server._evaluate_strategy_risk("user-1", "strat-123")
        assert exit_reason is None
        
    assert mock_db.strategy_positions.update_one.call_count == 2

    fallback_set = mock_db.strategy_positions.update_one.call_args_list[0][0][1]["$set"]
    assert fallback_set["r_initial_risk_amount"] == 10.0
    assert fallback_set["r_stop_loss_price"] == 85.0
    assert fallback_set["r_take_profit_price"] == 125.0
    assert fallback_set["r_metadata_source"] == "fallback"


# ── Trail / Breakeven tests ────────────────────────────────────────────────────

def test_trail_trigger_pct_set_by_exit_policy():
    """build_exit_policy must emit trail_trigger_pct and trail_step_pct, not just trailing_sl_pct."""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from exit_policy import build_exit_policy, TRAIL_AFTER_R

    policy = build_exit_policy(underlying_atr_pct=0.3, option_premium=100.0)
    assert policy is not None, "build_exit_policy returned None — check ATR/premium inputs"
    assert "trail_trigger_pct" in policy, "trail_trigger_pct missing from exit_policy output"
    assert "trail_step_pct" in policy, "trail_step_pct missing from exit_policy output"
    expected_trigger = round(policy["stop_loss_pct"] * TRAIL_AFTER_R, 2)
    assert policy["trail_trigger_pct"] == expected_trigger, (
        f"trail_trigger_pct={policy['trail_trigger_pct']} != stop_loss_pct*TRAIL_AFTER_R={expected_trigger}"
    )
    assert policy["trail_step_pct"] == round(policy["stop_loss_pct"] * 0.5, 2), \
        "trail_step_pct must be half of stop_loss_pct"
    print(f"PASS test_trail_trigger_pct_set_by_exit_policy "
          f"(sl={policy['stop_loss_pct']}%, trigger={policy['trail_trigger_pct']}%, step={policy['trail_step_pct']}%)")


def test_trail_activates_at_1r():
    """position_lifecycle: trail must NOT be active below +1R, must activate at exactly +1R.

    adaptive_exits_enabled=False is used so trigger_pct is taken verbatim (no clamping).
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from core.position_lifecycle import position_risk_prices

    pos = {
        "average_buy_price": 100.0,
        "position_side": "LONG",
        "tp_sl_tsl_config": {
            "stop_loss_pct": 10.0,
            "take_profit_pct": 15.0,
            "trail_trigger_pct": 10.0,  # activates at +10% = +1R
            "trail_step_pct": 5.0,
            "trailing_sl_enabled": True,
            "adaptive_exits_enabled": False,  # use raw trigger, no adaptive clamping
        },
    }

    # Below trigger: ltp = 109 (< entry * 1.10 = 110) — trail must NOT be active
    prices_below = position_risk_prices(pos, ltp=109.0)
    tsl_below = prices_below.get("trailing_sl")
    assert not tsl_below or tsl_below == 0, (
        f"Trail must not activate below trigger; trailing_sl={tsl_below}"
    )

    # At trigger: ltp = 112 (>= 110) — trail must be active
    prices_above = position_risk_prices(pos, ltp=112.0)
    tsl_above = prices_above.get("trailing_sl")
    assert tsl_above and tsl_above > 0, (
        f"Trail must activate at +1R; trailing_sl={tsl_above}"
    )
    print(f"PASS test_trail_activates_at_1r (tsl_below={tsl_below}, tsl_above={tsl_above:.2f})")


def test_trail_locks_breakeven():
    """position_lifecycle: once trail is active, trailing_sl must never be below entry."""
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from core.position_lifecycle import position_risk_prices

    entry = 100.0
    pos = {
        "average_buy_price": entry,
        "position_side": "LONG",
        "tp_sl_tsl_config": {
            "stop_loss_pct": 10.0,
            "take_profit_pct": 15.0,
            "trail_trigger_pct": 10.0,
            "trail_step_pct": 5.0,
            "trailing_sl_enabled": True,
            "adaptive_exits_enabled": False,  # raw trigger; activates at ltp >= 110
        },
    }

    # ltp=111: past trigger; candidate = 111 * 0.95 = 105.45, breakeven floor clamps to entry=100
    # Since 105.45 > 100, the floor doesn't lower it — but the key guarantee is tsl >= entry
    prices = position_risk_prices(pos, ltp=111.0)
    tsl = prices.get("trailing_sl")
    assert tsl is not None and tsl >= entry, (
        f"Trailing SL {tsl} must be >= entry {entry} once trail is active (breakeven floor)"
    )
    print(f"PASS test_trail_locks_breakeven (entry={entry}, ltp=111, tsl={tsl:.2f})")
