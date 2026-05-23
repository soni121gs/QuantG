"""Scratch validation script to verify dynamic MCX commodities resolution."""
from datetime import datetime, timezone
import sys
import os

# Adjust import path to include backend
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import _mcx_active_future_symbol, _upstox_instrument_token

def test_mcx_resolver():
    print("=== Testing MCX Active Future Symbol Resolver ===")
    
    # Test dates before 18th
    dt_mid = datetime(2026, 5, 12, tzinfo=timezone.utc)
    res_mid = _mcx_active_future_symbol("CRUDEOIL", dt_mid)
    print(f"Date: 2026-05-12 -> Result: {res_mid}")
    assert res_mid == "CRUDEOIL26MAYFUT", f"Expected CRUDEOIL26MAYFUT, got {res_mid}"
    
    # Test dates after 18th (should shift to next month contract)
    dt_end = datetime(2026, 5, 23, tzinfo=timezone.utc)
    res_end = _mcx_active_future_symbol("CRUDEOIL", dt_end)
    print(f"Date: 2026-05-23 (Today) -> Result: {res_end}")
    assert res_end == "CRUDEOIL26JUNFUT", f"Expected CRUDEOIL26JUNFUT, got {res_end}"
    
    print("[SUCCESS] test_mcx_resolver PASSED")

def test_upstox_token_resolver():
    print("\n=== Testing Upstox Instrument Token Resolver ===")
    
    # Test normal Equity symbol mapping
    tok_equity = _upstox_instrument_token("NSE", "RELIANCE")
    print(f"NSE RELIANCE -> {tok_equity}")
    assert tok_equity == "NSE_EQ|INE002A01018"
    
    # Test generic Commodity symbol mapping
    tok_crude = _upstox_instrument_token("MCX", "CRUDEOIL")
    print(f"MCX CRUDEOIL -> {tok_crude}")
    assert tok_crude.startswith("MCX_FO|CRUDEOIL"), f"Expected MCX_FO|CRUDEOIL... got {tok_crude}"
    
    # Test specific commodity future symbol mapping
    tok_specific = _upstox_instrument_token("MCX", "CRUDEOIL26JUNFUT")
    print(f"MCX CRUDEOIL26JUNFUT -> {tok_specific}")
    assert tok_specific == "MCX_FO|CRUDEOIL26JUNFUT"
    
    print("[SUCCESS] test_upstox_token_resolver PASSED")

if __name__ == "__main__":
    try:
        test_mcx_resolver()
        test_upstox_token_resolver()
        print("\nALL TESTS PASSED SUCCESSFULLY! The dynamic commodity systems are ready for production.")
    except AssertionError as exc:
        print(f"\nTEST FAILURE: {exc}")
        sys.exit(1)
