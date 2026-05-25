import pytest
import os
from safe_exec import safe_run_strategy

def test_safe_run_strategy_timeout():
    # Strategy that hangs in an infinite loop
    bad_code = """
def run(data):
    x = 0
    while True:
        x += 1
    return []
"""
    # Temporarily set STRATEGY_TIMEOUT_SEC to a low value (e.g., 0.2s) for fast testing
    os.environ["STRATEGY_TIMEOUT_SEC"] = "0.2"
    
    with pytest.raises(ValueError) as excinfo:
        safe_run_strategy(bad_code, [{"date": "2026-05-25 15:00", "close": 100.0}])
        
    assert "exceeded maximum safe time limit" in str(excinfo.value)
    
    # Restore default
    os.environ.pop("STRATEGY_TIMEOUT_SEC", None)

def test_safe_run_strategy_normal():
    # Healthy strategy
    good_code = """
def run(data):
    return [{"date": "2026-05-25 15:00", "action": "BUY"}]
"""
    result = safe_run_strategy(good_code, [{"date": "2026-05-25 15:00", "close": 100.0}])
    assert len(result) == 1
    assert result[0]["action"] == "BUY"
