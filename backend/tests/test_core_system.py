"""QuantG Unified Core Engine Verification Suite.

Validates domain schedule isolation, price routing logic, portfolio ledger constraints,
and live safety firewall protections.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta

from core.market_domains import resolve_domain_by_underlying, DomainType
from core.market_clock import get_segment_status
from core.price_service import PriceService
from core.portfolio_ledger import PortfolioLedger
from core.live_safety_firewall import LiveSafetyFirewall
from core.order_manager import OrderManager

def test_market_domain_isolation():
    """Asserts that only supported Upstox index domains resolve."""
    nifty_dom = resolve_domain_by_underlying("NIFTY")
    assert nifty_dom.name == DomainType.NSE_FO
    assert nifty_dom.exchange == "NFO"
    assert nifty_dom.get_lot_size("NIFTY") == 65

    sensex_dom = resolve_domain_by_underlying("SENSEX")
    assert sensex_dom.name == DomainType.BSE_FO
    assert sensex_dom.exchange == "BFO"
    assert sensex_dom.get_lot_size("SENSEX") == 20

def test_market_clock_schedules():
    """Validates configured Upstox index schedules."""
    # 20:00 IST (14:30 UTC) - Weekday
    utc_time = datetime(2026, 6, 1, 14, 30, tzinfo=timezone.utc)
    
    nse_status = get_segment_status(DomainType.NSE_FO, now_utc=utc_time)
    bse_status = get_segment_status(DomainType.BSE_FO, now_utc=utc_time)
    
    assert nse_status["open"] is False
    assert bse_status["open"] is False

@pytest.mark.asyncio
async def test_price_service_live_blocks_simulation():
    """Ensures the core price service throws exceptions if simulation feeds are passed while live."""
    db = MagicMock()
    service = PriceService(db)
    
    with pytest.raises(RuntimeError, match="Live price fetch failed"):
        await service.get_price("NSE:RELIANCE", mode="live", allow_simulation=True)

@pytest.mark.asyncio
async def test_portfolio_ledger_forces_fill_constraint():
    """Enforces the strict rule: No fill = no position. A position cannot exist without a fill."""
    db = MagicMock()
    db.strategy_positions.find_one = AsyncMock(return_value=None)
    db.strategy_positions.insert_one = AsyncMock()
    db.positions.update_one = AsyncMock()
    
    ledger = PortfolioLedger(db)
    
    fill = {
        "id": "fill_test_999",
        "user_id": "user-1",
        "strategy_id": "strategy-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26FEB24850CE",
        "side": "BUY",
        "qty": 65,
        "price": 120.0,
        "mode": "paper"
    }
    
    await ledger.process_fill(fill)
    
    # Assert database position insertion was triggered on receiving a fill
    db.strategy_positions.insert_one.assert_called_once()
    inserted_pos = db.strategy_positions.insert_one.call_args[0][0]
    assert inserted_pos["source_fill_id"] == "fill_test_999"
    assert inserted_pos["quantity"] == 65

def test_order_manager_idempotency():
    """Tests the secure compilation of composite idempotency keys."""
    mgr = OrderManager(MagicMock())
    key1 = mgr.generate_idempotency_key(
        strategy_id="strat-1",
        market_domain="NSE_FO",
        symbol="NIFTY",
        side="BUY",
        session_date="2026-06-01",
        signal_candle_time="14:30"
    )
    
    key2 = mgr.generate_idempotency_key(
        strategy_id="strat-1",
        market_domain="NSE_FO",
        symbol="NIFTY",
        side="BUY",
        session_date="2026-06-01",
        signal_candle_time="14:30"
    )
    
    assert key1 == key2
    
    key3 = mgr.generate_idempotency_key(
        strategy_id="strat-1",
        market_domain="NSE_FO",
        symbol="NIFTY",
        side="BUY",
        session_date="2026-06-01",
        signal_candle_time="14:35" # different candle time
    )
    
    assert key1 != key3
