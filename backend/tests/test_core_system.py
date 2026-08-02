"""QuantG Unified Core Engine Verification Suite.

Validates domain schedule isolation, price routing logic, portfolio ledger constraints,
and live safety firewall protections.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta

from core.market_domains import resolve_domain_by_underlying, DomainType
from core.market_clock import get_segment_status, is_trading_session_active
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


def test_strategy_runner_uses_strict_exchange_hours():
    # UTC times; IST = UTC+5:30. NSE F&O close moved 15:30 -> 15:40 IST on
    # 2026-08-03, so the runner window now ends at 10:10 UTC, not 10:00.
    assert is_trading_session_active(datetime(2026, 7, 6, 3, 44, tzinfo=timezone.utc)) is False  # 09:14
    assert is_trading_session_active(datetime(2026, 7, 6, 3, 45, tzinfo=timezone.utc)) is True   # 09:15
    assert is_trading_session_active(datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)) is True   # 15:30
    assert is_trading_session_active(datetime(2026, 7, 6, 10, 10, tzinfo=timezone.utc)) is True  # 15:40
    assert is_trading_session_active(datetime(2026, 7, 6, 10, 11, tzinfo=timezone.utc)) is False # 15:41
    assert is_trading_session_active(datetime(2026, 7, 5, 6, 0, tzinfo=timezone.utc)) is False   # Sunday

@pytest.mark.asyncio
async def test_price_service_live_blocks_simulation():
    """Ensures the core price service throws exceptions if simulation feeds are passed while live."""
    db = MagicMock()
    service = PriceService(db)
    
    with pytest.raises(RuntimeError, match="Live price fetch failed"):
        await service.get_price("NSE:RELIANCE", mode="live", allow_simulation=True)

def _ledger_db_mock():
    """MagicMock db with every collection method the ledger touches awaitable."""
    db = MagicMock()
    db.processed_fill_ids.insert_one = AsyncMock()
    db.processed_fill_ids.delete_one = AsyncMock()
    db.strategy_positions.find_one = AsyncMock(return_value=None)
    db.strategy_positions.insert_one = AsyncMock()
    db.strategy_positions.update_one = AsyncMock()
    db.positions.update_one = AsyncMock()
    db.positions.delete_one = AsyncMock()
    db.trade_fills.insert_one = AsyncMock()
    db.trades.insert_one = AsyncMock()
    db.strategies.update_one = AsyncMock()
    db.strategy_loss_streaks.update_one = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_portfolio_ledger_forces_fill_constraint():
    """Enforces the strict rule: No fill = no position. A position cannot exist without a fill."""
    db = _ledger_db_mock()
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

    result = await ledger.process_fill(fill)

    # Assert database position insertion was triggered on receiving a fill
    db.strategy_positions.insert_one.assert_called_once()
    inserted_pos = db.strategy_positions.insert_one.call_args[0][0]
    assert inserted_pos["source_fill_id"] == "fill_test_999"
    assert inserted_pos["quantity"] == 65
    # Spine contract: result dict + audit row
    assert result["accepted"] is True
    assert result["action"] == "OPEN"
    db.trade_fills.insert_one.assert_called_once()


@pytest.mark.asyncio
async def test_portfolio_ledger_exit_fill_matches_exiting_position():
    """THE stuck-position fix: an exit fill must close a position in EXITING status."""
    db = _ledger_db_mock()
    exiting_pos = {
        "id": "pos_abc",
        "user_id": "user-1",
        "strategy_id": "strategy-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26FEB24850CE",
        "mode": "paper",
        "status": "EXITING",
        "position_side": "LONG",
        "open_quantity": 65,
        "average_price": 120.0,
        "average_buy_price": 120.0,
        "entry_charges": 30.0,
        "entry_time": "2026-06-10T04:00:00+00:00",
    }
    db.strategy_positions.find_one = AsyncMock(return_value=exiting_pos)
    ledger = PortfolioLedger(db)

    result = await ledger.process_fill({
        "id": "fill_exit_1",
        "user_id": "user-1",
        "strategy_id": "strategy-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26FEB24850CE",
        "side": "SELL",
        "qty": 65,
        "price": 130.0,
        "charges": 40.0,
        "mode": "paper",
        "exit_reason": "take-profit",
    })

    assert result["accepted"] is True
    assert result["action"] == "CLOSE"
    assert result["qty_closed"] == 65
    # net = (130-120)*65 - 40 exit charges - 30 entry charges = 580
    assert result["realized_pnl"] == pytest.approx(580.0)
    # The EXITING→OPEN query must include EXITING
    query = db.strategy_positions.find_one.call_args[0][0]
    assert "EXITING" in query["status"]["$in"]
    # Round-trip trade row written on close
    db.trades.insert_one.assert_called_once()
    trade_row = db.trades.insert_one.call_args[0][0]
    assert trade_row["net_pnl"] == pytest.approx(580.0)
    assert trade_row["exit_reason"] == "take-profit"


@pytest.mark.asyncio
async def test_portfolio_ledger_rejects_duplicate_exit_fill():
    """A SELL fill with no live position but a CLOSED LONG = duplicate exit → REJECTED, not a phantom SHORT."""
    db = _ledger_db_mock()

    async def find_one_side_effect(query, *a, **k):
        status = query.get("status")
        if isinstance(status, dict) and "EXITING" in status.get("$in", []):
            return None  # no live position
        if status == "CLOSED":
            return {"id": "pos_closed", "position_side": "LONG", "status": "CLOSED"}
        return None

    db.strategy_positions.find_one = AsyncMock(side_effect=find_one_side_effect)
    ledger = PortfolioLedger(db)

    result = await ledger.process_fill({
        "id": "fill_dup_exit",
        "user_id": "user-1",
        "strategy_id": "strategy-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26FEB24850CE",
        "side": "SELL",
        "qty": 65,
        "price": 130.0,
        "mode": "paper",
    })

    assert result["accepted"] is False
    assert result["action"] == "DUPLICATE_EXIT"
    # No position created, no trade written
    db.strategy_positions.insert_one.assert_not_called()
    db.trades.insert_one.assert_not_called()

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
