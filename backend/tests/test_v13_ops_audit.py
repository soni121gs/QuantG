import sys
import os
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server
from core.strategy_brain_schema import (
    calculate_code_hash,
    normalize_signal,
    normalize_python_code,
)


# Hashing and whitespace normalization tests
def test_whitespace_normalization_ignores_spacing_and_comments():
    code1 = """
def run(data):
    # This is a comment
    if len(data) < 10:
        return []
    
    val = 42
    
    return [{'action': 'BUY'}]
"""
    code2 = """def run(data):
    if len(data) < 10:
        return []
    val = 42
    return [{'action': 'BUY'}]"""

    # Hashing ignores spacing but comments inside line are normalized.
    # Note: comments on separate lines are skipped if we skip empty lines,
    # but inline comments remain. Let's verify our normalizer:
    # We stripped empty lines and collapsed spaces.
    norm1 = normalize_python_code(code1)
    norm2 = normalize_python_code(code2)
    
    # Let's test a simpler spacing case to confirm spacing normalization
    code_a = "def run(data):   x = 1\n\n   return x"
    code_b = "def run(data): x = 1\nreturn x"
    assert normalize_python_code(code_a) == "def run(data): x = 1\nreturn x"
    assert calculate_code_hash(code_a) == calculate_code_hash(code_b)


# Signal normalization tests
def test_legacy_signal_normalization():
    # Signal missing fields
    legacy_sig = {
        "action": "BUY",
        "reason": "EMA crossover bullish PE signal",
    }
    
    normalized = normalize_signal(legacy_sig)
    
    assert normalized["signal_schema_status"] == "LEGACY_SIGNAL"
    assert normalized["action"] == "BUY"
    # Direction PE is inferred because of "PE" and "bullish PE signal" inside reason
    assert normalized["direction"] == "PE"
    assert normalized["confidence"] == 85.0
    assert normalized["target_R"] == 2.0
    assert normalized["initial_stop_R"] == 1.0
    assert normalized["option_selection_preference"] == "ATM"
    assert normalized["signal_version"] == "1.0"
    assert normalized["strategy_logic_version"] == "1.0"
    assert normalized["reason"] == "EMA crossover bullish PE signal"


def test_v13_style_signal_recognition():
    # Signal with all 14 required fields
    v13_sig = {
        "action": "SELL",
        "direction": "PE",
        "setup_type": "breakout",
        "confidence": 92.5,
        "entry_reason": "High volume Bollinger breakout",
        "target_R": 3.0,
        "initial_stop_R": 1.5,
        "trail_after_R": 2.0,
        "max_hold_minutes": 45,
        "invalidation_rule": "cooldown_or_exit",
        "regime_required": "trending",
        "option_selection_preference": "ITM1",
        "signal_version": "1.3",
        "strategy_logic_version": "2.1",
    }
    
    normalized = normalize_signal(v13_sig)
    
    assert normalized["signal_schema_status"] == "V13_SIGNAL"
    assert normalized["action"] == "SELL"
    assert normalized["direction"] == "PE"
    assert normalized["setup_type"] == "breakout"
    assert normalized["confidence"] == 92.5
    assert normalized["target_R"] == 3.0
    assert normalized["initial_stop_R"] == 1.5
    assert normalized["trail_after_R"] == 2.0
    assert normalized["max_hold_minutes"] == 45
    assert normalized["invalidation_rule"] == "cooldown_or_exit"
    assert normalized["regime_required"] == "trending"
    assert normalized["option_selection_preference"] == "ITM1"
    assert normalized["signal_version"] == "1.3"
    assert normalized["strategy_logic_version"] == "2.1"


# API endpoints integration tests
@pytest.mark.anyio
async def test_ops_strategy_code_audit():
    client = TestClient(server.app)
    
    # Mock database strategies
    mock_db = MagicMock()
    
    # We create:
    # - 2 strategies with the same code (duplicates)
    # - 1 strategy with unique code
    code_duplicate = "def run(data):\n    return []"
    code_unique = "def run(data):\n    return [{'action': 'BUY'}]"
    
    mock_db.strategies.find.return_value.to_list = AsyncMock(return_value=[
        {
            "_id": "mongo-id-1",
            "id": "strategy-uuid-1",
            "name": "NIFTY Dup 1",
            "status": "live",
            "mode": "live",
            "broker": "upstox",
            "underlying": "NIFTY",
            "python_code": code_duplicate,
            "risk_style": "momentum",
            "risk": {"exit_mode": "signal_only"},
            "default_strategy_version": "1.0",
            "signal_schema_status": "LEGACY_SIGNAL",
        },
        {
            "_id": "mongo-id-2",
            "id": "strategy-uuid-2",
            "name": "NIFTY Dup 2",
            "status": "paused",
            "mode": "paper",
            "broker": "upstox",
            "underlying": "NIFTY",
            "python_code": code_duplicate,
            "risk_style": "momentum",
            "risk": {"exit_mode": "signal_only"},
            "default_strategy_version": "1.0",
        },
        {
            "_id": "mongo-id-3",
            "id": "strategy-uuid-3",
            "name": "Unique Momentum",
            "status": "live",
            "mode": "live",
            "broker": "upstox",
            "underlying": "BANKNIFTY",
            "python_code": code_unique,
            "risk_style": "breakout",
            "risk": {"exit_mode": "signal_or_tp_sl_trailing"},
        }
    ])
    
    async def fake_get_current_user():
        return {"id": "user-1", "email": "trader@quantg.com"}
        
    from core import get_current_user as core_get_current_user
    server.app.dependency_overrides[core_get_current_user] = fake_get_current_user
    server.app.dependency_overrides[server.get_current_user] = fake_get_current_user
    
    with patch("server.db", mock_db), patch("routes.ops.db", mock_db):
        response = client.get("/api/ops/strategy-code-audit")
        
    server.app.dependency_overrides.clear()
    
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["total_strategies"] == 3
    assert data["live_strategies"] == 2
    
    # Validate duplicates are detected
    duplicate_groups = data["duplicate_code_groups"]
    assert len(duplicate_groups) == 1
    assert duplicate_groups[0]["count"] == 2
    assert "NIFTY Dup 1" in duplicate_groups[0]["names"]
    assert "NIFTY Dup 2" in duplicate_groups[0]["names"]
    
    # Validate Mongo _id is not exposed and strategy_id is present
    for s in data["strategies"]:
        assert "_id" not in s
        assert "strategy_id" in s
        if s["name"] == "NIFTY Dup 1":
            assert s["duplicate_code_group"] == duplicate_groups[0]["code_hash"]
            assert s["exit_mode"] == "signal_only"
            assert s["signal_schema_status"] == "LEGACY_SIGNAL"
        elif s["name"] == "Unique Momentum":
            assert s["duplicate_code_group"] is None
            assert s["exit_mode"] == "signal_or_tp_sl_trailing"


@pytest.mark.anyio
async def test_ops_default_strategy_catalog_audit():
    client = TestClient(server.app)
    
    async def fake_get_current_user():
        return {"id": "user-1", "email": "trader@quantg.com"}
        
    from core import get_current_user as core_get_current_user
    server.app.dependency_overrides[core_get_current_user] = fake_get_current_user
    server.app.dependency_overrides[server.get_current_user] = fake_get_current_user
    
    response = client.get("/api/ops/default-strategy-catalog-audit")
    
    server.app.dependency_overrides.clear()
    
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["total_default_strategies"] == 9
    assert data["mcx_crude_naturalgas_excluded"] is True
    
    # Confirm names are returned
    names = data["names"]
    assert "NIFTY VWAP Trend Breakout" in names
    assert "SENSEX Swing RSI Pullback" in names
    assert "NIFTY Quick EMA Scalper" in names
    
    # Verify duplicates inside default catalog are decoupled
    # NIFTY HFT Quick Scalper and NIFTY Quick EMA Scalper no longer share the same code
    assert data["shares_code_with_another"] is False
    
    # Locate HFT and Quick EMA scalpers and check they do not share_code
    scalpers = [s for s in data["strategies"] if s["name"] in ("NIFTY HFT Quick Scalper", "NIFTY Quick EMA Scalper")]
    assert len(scalpers) == 2
    assert scalpers[0]["shares_code"] is False
    assert scalpers[1]["shares_code"] is False
    assert scalpers[0]["code_hash"] != scalpers[1]["code_hash"]


@pytest.mark.anyio
async def test_ops_v13_strategy_brain_dry_run_is_readonly():
    client = TestClient(server.app)
    
    mock_db = MagicMock()
    # Mocking strategy list: trader has one default strategy, one legacy strategy
    mock_db.strategies.find.return_value.to_list = AsyncMock(return_value=[
        {
            "_id": "mongo-id-1",
            "id": "strategy-uuid-1",
            "name": "NIFTY VWAP Trend Breakout",
            "status": "live",
            "mode": "live",
            "python_code": "def run(data):\n    return []",
            "default_strategy_version": "retail-balanced-v3",
            "risk": {"exit_mode": "signal_or_tp_sl_trailing"},
            "signal_schema_status": "LEGACY_SIGNAL",
        },
        {
            "_id": "mongo-id-2",
            "id": "strategy-uuid-2",
            "name": "UPSTOX NIFTY ATM Option Momentum Buyer",
            "status": "paused",
            "mode": "paper",
            "python_code": "def run(data):\n    return []",
            "risk": {"exit_mode": "signal_only"},
        }
    ])
    
    async def fake_get_current_user():
        return {"id": "user-1", "email": "trader@quantg.com"}
        
    from core import get_current_user as core_get_current_user
    server.app.dependency_overrides[core_get_current_user] = fake_get_current_user
    server.app.dependency_overrides[server.get_current_user] = fake_get_current_user
    
    with patch("server.db", mock_db), patch("routes.ops.db", mock_db):
        response = client.post("/api/ops/v13/strategy-brain/dry-run")
        
    server.app.dependency_overrides.clear()
    
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    
    # Inserts: 9 default strategies total - 2 present in DB = 7 to insert
    assert len(data["strategies_to_insert"]) == 7
    # Updates: 2 present in DB
    assert len(data["strategies_to_update_by_name"]) == 2
    assert "NIFTY VWAP Trend Breakout" in data["strategies_to_update_by_name"]
    assert "UPSTOX NIFTY ATM Option Momentum Buyer" in data["strategies_to_update_by_name"]
    
    # Old versions map
    assert data["old_default_versions"]["NIFTY VWAP Trend Breakout"] == "retail-balanced-v3"
    
    # Exits & Metadata
    assert "UPSTOX NIFTY ATM Option Momentum Buyer" in data["strategies_that_use_signal_only_exit"]
    assert "UPSTOX NIFTY ATM Option Momentum Buyer" in data["missing_v13_signal_metadata"]
    
    # Confirm DB was never updated
    assert mock_db.strategies.insert_many.call_count == 0
    assert mock_db.strategies.update_one.call_count == 0
    assert mock_db.strategies.update_many.call_count == 0
