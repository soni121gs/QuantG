import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution_bridge import ExecutionDispatchPayload, runtime_broker, submit_order


@pytest.mark.asyncio
async def test_submit_order_rejects_legacy_broker_before_adapter_call():
    async def place_upstox(*args, **kwargs):
        raise AssertionError("Upstox adapter should not be called for legacy broker")

    async def legacy_adapter(*args, **kwargs):
        raise AssertionError("Legacy broker adapter must never be called")

    payload = ExecutionDispatchPayload(
        broker="zerodha",
        segment="NSE_FO",
        exchange="NFO",
        tradingsymbol="NIFTY26FEB24850CE",
        instrument_token="NSE_FO|12345",
        quantity=65,
        side="BUY",
    )

    result = await submit_order(
        "user-1",
        payload,
        place_upstox=place_upstox,
        place_kite=legacy_adapter,
        resolve_upstox_token=lambda *_: "NSE_FO|12345",
    )

    assert result["ok"] is False
    assert result["status"] == "FAILED"
    assert "Unsupported broker" in result["error"]


def test_runtime_broker_defaults_to_upstox_only():
    assert runtime_broker("") == "upstox"
    assert runtime_broker("upstox") == "upstox"
    assert runtime_broker("kotak_neo") == "kotak_neo"
