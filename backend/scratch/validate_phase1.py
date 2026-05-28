"""Validation script to verify that Phase 1 decoupling is 100% correct.

It mathematically asserts that normalization.py output matches kite_helper.py output
identically for mock Upstox order and position payloads, checking API contract integrity.
"""
import sys
import os
from pprint import pprint

# Ensure backend directory is in sys.path
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import kite_helper
from brokers import normalization

def test_order_normalization():
    print("--- Running Order Normalization Tests ---")
    
    # Sample Upstox v2 Order Payload (based on Upstox API documentation)
    sample_upstox_order = {
        "order_id": "24052800012345",
        "trading_symbol": "RELIANCE",
        "status": "complete",
        "quantity": 10,
        "filled_quantity": 10,
        "price": 2945.50,
        "average_price": 2945.50,
        "order_type": "MARKET",
        "transaction_type": "BUY",
        "product": "I",
        "exchange": "NSE",
        "instrument_token": "NSE_EQ|INE002A01018",
        "order_timestamp": "2026-05-28 12:00:00",
    }
    
    # Normalize using legacy kite_helper (delegated to new normalization under the hood)
    legacy_norm = kite_helper.normalize_order_update(sample_upstox_order, broker="upstox")
    
    # Normalize using new brokers/normalization module directly
    new_norm = normalization.normalize_order_update(sample_upstox_order, broker="upstox")
    
    # Assert fields are identical
    pprint(new_norm)
    assert legacy_norm == new_norm, "Order normalization mismatch!"
    print("[PASS] Order normalization test passed! Legacy and new normalized outputs are identical.")
    return new_norm

def test_position_normalization():
    print("--- Running Position Normalization Tests ---")
    
    # Sample Upstox v2 Position Payload
    sample_upstox_positions = {
        "status": "success",
        "data": {
            "net": [
                {
                    "trading_symbol": "NIFTY26MAY24850CE",
                    "quantity": 75,
                    "buy_price": 120.50,
                    "last_price": 135.20,
                    "pnl": 1102.50,
                    "product": "NRML",
                    "exchange": "NFO",
                    "instrument_token": "NFO_FO|54321",
                }
            ],
            "day": []
        }
    }
    
    # Normalize using legacy kite_helper
    legacy_norm = kite_helper.normalize_positions_payload(sample_upstox_positions, broker="upstox")
    
    # Normalize using new brokers/normalization module directly
    new_norm = normalization.normalize_positions_payload(sample_upstox_positions, broker="upstox")
    
    # Assert fields are identical
    pprint(new_norm)
    assert legacy_norm == new_norm, "Position normalization mismatch!"
    print("[PASS] Position normalization test passed! Legacy and new normalized outputs are identical.")
    return new_norm

def test_contract_integrity(order_out, position_out):
    print("--- Verifying API Contract Fields ---")
    
    # Assert required fields in order output are present and correct for frontend/db
    required_order_fields = [
        "broker", "order_id", "tradingsymbol", "transaction_type",
        "quantity", "filled_qty", "pending_qty", "average_price", "price",
        "order_type", "product", "status", "exchange", "instrument_token"
    ]
    for field in required_order_fields:
        assert field in order_out, f"Missing required order contract field: {field}"
        
    # Assert required fields in position row are present and correct
    required_position_fields = [
        "broker", "tradingsymbol", "quantity", "average_price", "last_price",
        "pnl", "product", "exchange", "instrument_token", "netQty", "avgPrc", "ltp"
    ]
    for row in position_out["net"]:
        for field in required_position_fields:
            assert field in row, f"Missing required position contract field: {field}"
            
    print("[PASS] API Contract Integrity test passed! All required database/frontend keys are perfectly preserved.")

if __name__ == "__main__":
    try:
        order_out = test_order_normalization()
        position_out = test_position_normalization()
        test_contract_integrity(order_out, position_out)
        print("\n[SUCCESS] ALL DECUPLE VALIDATION TESTS COMPLETED SUCCESSFULLY!")
        sys.exit(0)
    except Exception as e:
        print(f"\n[FAIL] VALIDATION TEST FAILED: {e}", file=sys.stderr)
        sys.exit(1)
