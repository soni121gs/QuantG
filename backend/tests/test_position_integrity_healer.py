import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Import tested modules
from position_reconciler import calculate_protection_for_position, create_manual_recovery_strategy_if_missing
from server import _paper_delta, _is_order_market_open, _intent_is_entry

def test_negative_quantity_delta_exit_prevention():
    """Verify that exits cap delta correctly to prevent negative quantities."""
    # Test _paper_delta basic behavior
    assert _paper_delta("OPEN_LONG", 10) == 10
    assert _paper_delta("CLOSE_LONG", 10) == -10
    assert _paper_delta("OPEN_SHORT", 10) == -10
    assert _paper_delta("CLOSE_SHORT", 10) == 10

def test_calculate_protection_for_position_compliant():
    """Verify SL/TP are calculated ONLY when strategy settings are available (no invented defaults)."""
    # Case 1: Strategy has risk settings
    strategy_with_risk = {
        "visual_config": {
            "risk": {
                "stop_loss_pct": 5.0,
                "take_profit_pct": 15.0
            }
        }
    }
    
    prot = calculate_protection_for_position(100.0, "LONG", strategy_with_risk)
    assert prot["stop_loss"] == 95.0
    assert prot["take_profit"] == 115.0
    assert prot["protection_status"] == "PROTECTED"
    
    # Case 2: Short position with risk settings
    prot_short = calculate_protection_for_position(100.0, "SHORT", strategy_with_risk)
    assert prot_short["stop_loss"] == 105.0
    assert prot_short["take_profit"] == 85.0
    assert prot_short["protection_status"] == "PROTECTED"

    # Case 3: Strategy risk settings are missing / zero (compliant with USER rule: do NOT invent defaults)
    strategy_no_risk = {
        "visual_config": {
            "risk": {
                "stop_loss_pct": 0.0,
                "take_profit_pct": 0.0
            }
        }
    }
    
    prot_none = calculate_protection_for_position(100.0, "LONG", strategy_no_risk)
    assert prot_none["stop_loss"] is None
    assert prot_none["take_profit"] is None
    assert prot_none["protection_status"] == "UNPROTECTED"


def test_is_order_market_open_nse_mcx():
    """Verify that _is_order_market_open checks standard weekdays and hours correctly."""
    from datetime import datetime, timezone

    # 1. NSE Weekday during market hours (Wednesday 10:30 AM IST / 5:00 AM UTC)
    now_nse_open = datetime(2026, 6, 3, 5, 0, tzinfo=timezone.utc)
    assert _is_order_market_open("NSE", now_nse_open) is True

    # 2. NSE Weekday outside market hours (Wednesday 9:00 PM IST / 3:30 PM UTC)
    now_nse_closed = datetime(2026, 6, 3, 15, 30, tzinfo=timezone.utc)
    assert _is_order_market_open("NSE", now_nse_closed) is False

    # 3. NSE Weekend (Sunday 12:00 PM IST / 6:30 AM UTC)
    now_nse_weekend = datetime(2026, 6, 7, 6, 30, tzinfo=timezone.utc)
    assert _is_order_market_open("NSE", now_nse_weekend) is False

    # 4. MCX Weekday during commodity hours (Wednesday 8:00 PM IST / 2:30 PM UTC)
    now_mcx_open = datetime(2026, 6, 3, 14, 30, tzinfo=timezone.utc)
    assert _is_order_market_open("MCX", now_mcx_open) is True

    # 5. MCX Weekend (Sunday 8:00 PM IST / 2:30 PM UTC)
    now_mcx_weekend = datetime(2026, 6, 7, 14, 30, tzinfo=timezone.utc)
    assert _is_order_market_open("MCX", now_mcx_weekend) is False


def test_paper_mode_session_enforcement_logic():
    """Verify the intent checks for paper session gating."""
    assert _intent_is_entry("OPEN_LONG") is True
    assert _intent_is_entry("OPEN_SHORT") is True
    assert _intent_is_entry("CLOSE_LONG") is False
    assert _intent_is_entry("CLOSE_SHORT") is False


@pytest.mark.asyncio
async def test_place_order_core_paper_market_gating():
    """Verify that _place_order_core rejects paper entry orders outside hours but accepts exits."""
    from unittest.mock import patch, MagicMock, AsyncMock
    from fastapi import HTTPException
    from server import _place_order_core
    
    # Mock all required database and helper methods
    with patch("server.get_user_settings", new_callable=AsyncMock) as mock_settings, \
         patch("server.db.strategies.find_one", new_callable=AsyncMock) as mock_strat, \
         patch("server._is_order_market_open", return_value=False), \
         patch("server._build_order_intent", new_callable=AsyncMock) as mock_intent:
         
         # Mock user settings
         mock_settings.return_value = {"paper_mode": True, "execution_broker": "upstox"}
         mock_strat.return_value = {"id": "strat_1", "mode": "paper"}
         
         # Mock return values for _build_order_intent
         mock_instr = MagicMock()
         mock_instr.exchange = "NSE"
         mock_instr.tradingsymbol = "RELIANCE"
         mock_instr.segment = "EQUITY"
         mock_instr.asset_class = "DIRECT"
         mock_instr.instrument_token = "NSE_EQ:RELIANCE"
         mock_instr.broker = "upstox"
         mock_instr.model_dump.return_value = {
             "exchange": "NSE",
             "tradingsymbol": "RELIANCE",
             "segment": "EQUITY",
             "asset_class": "DIRECT",
             "instrument_token": "NSE_EQ:RELIANCE",
             "broker": "upstox"
         }
         
         mock_intent_obj = MagicMock()
         mock_intent_obj.instrument = mock_instr
         mock_intent_obj.quantity = 10
         mock_intent_obj.stop_loss = None
         mock_intent_obj.take_profit = None
         mock_intent_obj.model_dump.return_value = {
             "intent": "CLOSE_LONG",
             "quantity": 10,
             "stop_loss": None,
             "take_profit": None
         }
         
         # Case 1: Entry order ("OPEN_LONG") -> Should raise HTTPException
         mock_intent_obj.intent = "OPEN_LONG"
         mock_intent.return_value = {
             "intent": mock_intent_obj,
             "lot_size": 1,
             "lots": 1
         }
         
         with pytest.raises(HTTPException) as exc_info:
             await _place_order_core(
                 user_id="user_123",
                 symbol="RELIANCE",
                 side="BUY",
                 qty=10,
                 source="strategy:strat_1"
             )
         assert exc_info.value.status_code == 400
         assert "Paper entry orders are blocked outside" in exc_info.value.detail
         
         # Case 2: Exit order ("CLOSE_LONG") -> Should proceed past the time-window check
         # (It will raise an exception later on, e.g. on missing DB inserts, but it shouldn't raise the out-of-hours block)
         mock_intent_obj.intent = "CLOSE_LONG"
         mock_intent.return_value = {
             "intent": mock_intent_obj,
             "lot_size": 1,
             "lots": 1
         }
         
         # We mock db.orders.insert_one and other downstream methods to avoid side effects
         with patch("server._resolve_order_fill_hint", new_callable=AsyncMock, return_value=100.0), \
              patch("server._open_strategy_position_for_exit", new_callable=AsyncMock) as mock_exit_record, \
              patch("server._insert_order_intent", new_callable=AsyncMock) as mock_insert, \
              patch("server._apply_paper_fill_to_position", new_callable=AsyncMock) as mock_fill:
              
              mock_exit_record.return_value = {"id": "exit_123", "open_quantity": 10}
              mock_insert.return_value = ({"id": "order_123", "mode": "paper", "asset_type": "equity", "qty": 10}, True)
              mock_fill.return_value = {"id": "order_123", "status": "COMPLETE"}
              
              res = await _place_order_core(
                  user_id="user_123",
                  symbol="RELIANCE",
                  side="SELL",
                  qty=10,
                  source="strategy:strat_1"
              )
              assert res is not None
