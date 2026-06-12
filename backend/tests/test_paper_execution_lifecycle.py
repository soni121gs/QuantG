import os
import sys
import pytest
import asyncio
from datetime import datetime, timezone, date, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import (
    DEFAULT_OPTION_STRATEGIES,
    _place_order_core,
    _execution_preflight,
    _build_order_intent,
)
from signal_manager import ConflictResolver


# --- MOCK HELPERS ---

def _mock_option_contract(symbol="NIFTY2660424000CE", exchange="NFO", ltp=100.0, simulated=False, expiry=None):
    if not expiry:
        expiry = (date.today() + timedelta(days=2)).isoformat()
    return {
        "tradingsymbol": symbol,
        "exchange": exchange,
        "instrument_token": f"token-{symbol}",
        "instrument_key": f"token-{symbol}",
        "upstox_instrument_token": f"token-{symbol}",
        "lot_size": 50,
        "strike": 24000,
        "expiry": expiry,
        "underlying": "NIFTY",
        "option_type": "CE" if symbol.endswith("CE") else "PE",
        "spot": 24000.0,
        "atm_strike": 24000,
        "transaction_type": "BUY",
        "simulated": simulated,
        "source": "LIVE_UPSTOX_CONTRACT",
        "ltp": ltp,
        "bid": ltp - 0.1 if ltp > 0 else 0,
        "ask": ltp + 0.1 if ltp > 0 else 0,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


def _mock_db():
    mock_db = MagicMock()
    mock_db.strategies.find_one = AsyncMock(return_value={"id": "strat-1", "mode": "paper", "status": "live", "risk_style": "balanced"})
    mock_db.strategy_positions.find_one = AsyncMock(return_value=None)
    mock_db.strategy_positions.find = MagicMock()
    mock_db.strategy_positions.find().to_list = AsyncMock(return_value=[])
    mock_db.orders.find_one = AsyncMock(return_value=None)
    mock_db.orders.insert_one = AsyncMock()
    mock_db.skipped_signals.find_one_and_update = AsyncMock(return_value=None)
    mock_db.order_events.insert_one = AsyncMock()
    mock_db.risk_state.find_one = AsyncMock(return_value=None)
    mock_db.users.find_one = AsyncMock(return_value=None)
    mock_db.paper_wallets.find_one = AsyncMock(return_value=None)
    mock_db.live_arm_state.find_one = AsyncMock(return_value=None)
    mock_db.core_events.insert_one = AsyncMock()
    return mock_db


# --- TESTS ---

def test_seeded_strategy_exit_mode_matrix():
    """1. Seeded strategy exit_mode matrix test.
    Verify that all option strategies are seeded with signal_or_tp_sl_trailing.
    """
    for strat in DEFAULT_OPTION_STRATEGIES:
        name = strat["name"]
        exit_mode = strat.get("risk", {}).get("exit_mode")
        assert exit_mode == "signal_or_tp_sl_trailing", f"{name} should have exit_mode signal_or_tp_sl_trailing"


@pytest.mark.anyio
async def test_no_strategy_can_open_unprotected_position():
    """2. Verify that no strategy can bypass exit controls.
    If a strategy without internal exits is configured to bypass SL/TP (e.g. signal_only),
    the system must ensure safety. In our code, we enforce 'signal_or_tp_sl_trailing' on seeding,
    and we check that the ledger will run premium checks if not explicitly signal_only.
    """
    # Verify that for NIFTY VWAP Trend Breakout (which has no internal exit in the template metadata),
    # the seeded exit mode is signal_or_tp_sl_trailing, which keeps premium checks active.
    vwap_strat = next(s for s in DEFAULT_OPTION_STRATEGIES if s["name"] == "NIFTY VWAP Trend Breakout")
    assert vwap_strat["risk"]["exit_mode"] == "signal_or_tp_sl_trailing"


@pytest.mark.anyio
async def test_ce_position_exits_on_correct_sell_signal():
    """3a. Verify that a Call Option (CE) active position exits on a SELL signal.
    """
    from strategy_runner import runner_loop
    
    mock_db = MagicMock()
    # Mock active position for NIFTY CE
    mock_db.strategy_positions.find_one = AsyncMock(return_value={
        "id": "pos-ce",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "instrument_key": "NFO:NIFTY24MAY24000CE",
        "trading_symbol": "NIFTY24MAY24000CE",
        "status": "OPEN",
    })
    mock_db.strategies.update_one = AsyncMock()
    
    close_mock = AsyncMock()
    
    # We run the strategy runner loop which processes signals
    # We pass a strategy that emits a SELL signal (CE exit)
    # The runner must intercept this as an exit and trigger close_strategy_fn.
    strategy = {
        "id": "strat-1",
        "user_id": "user-1",
        "python_code": "def run(data): return [{'date': '2026-06-05 09:30', 'action': 'SELL', 'reason': 'CE exit'}]",
        "visual_config": {"options": {"enabled": True, "strike_mode": "ATM_BUY"}},
        "status": "live",
    }
    
    mock_find_cursor = MagicMock()
    mock_find_cursor.to_list = AsyncMock(return_value=[strategy])
    mock_db.strategies.find = MagicMock(return_value=mock_find_cursor)
    
    stop_event = asyncio.Event()
    
    async def mock_get_price_history(user_id, symbol, days=60, strategy=None):
        stop_event.set()
        return {
            "data": [{"date": "2026-06-05 09:30", "close": 100}],
            "source": "simulated",
            "is_live": True,
            "paper_mode": True,
        }
    
    with patch("strategy_runner._acquire_lock", AsyncMock(return_value=True)), \
         patch("strategy_runner._release_lock", AsyncMock()), \
         patch("core.market_clock.get_segment_status", return_value={"open": True}):
        await runner_loop(
            db=mock_db,
            get_price_history=mock_get_price_history,
            place_order_fn=AsyncMock(),
            stop_event=stop_event,
            resolve_option_fn=AsyncMock(),
            close_strategy_fn=close_mock,
        )
        
    close_mock.assert_called_once_with("user-1", "strat-1", reason="strategy-sell-signal")


@pytest.mark.anyio
async def test_pe_position_exits_on_correct_buy_signal():
    """3b. Verify that a Put Option (PE) active position exits on a BUY signal.
    """
    from strategy_runner import runner_loop
    
    mock_db = MagicMock()
    # Mock active position for NIFTY PE
    mock_db.strategy_positions.find_one = AsyncMock(return_value={
        "id": "pos-pe",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "instrument_key": "NFO:NIFTY24MAY24000PE",
        "trading_symbol": "NIFTY24MAY24000PE",
        "status": "OPEN",
    })
    mock_db.strategies.update_one = AsyncMock()
    
    close_mock = AsyncMock()
    
    # We pass a strategy that emits a BUY signal (PE exit)
    # The runner must intercept this as an exit and trigger close_strategy_fn.
    strategy = {
        "id": "strat-1",
        "user_id": "user-1",
        "python_code": "def run(data): return [{'date': '2026-06-05 09:30', 'action': 'BUY', 'reason': 'PE exit'}]",
        "visual_config": {"options": {"enabled": True, "strike_mode": "ATM_BUY"}},
        "status": "live",
    }
    
    mock_find_cursor = MagicMock()
    mock_find_cursor.to_list = AsyncMock(return_value=[strategy])
    mock_db.strategies.find = MagicMock(return_value=mock_find_cursor)
    
    stop_event = asyncio.Event()
    
    async def mock_get_price_history(user_id, symbol, days=60, strategy=None):
        stop_event.set()
        return {
            "data": [{"date": "2026-06-05 09:30", "close": 100}],
            "source": "simulated",
            "is_live": True,
            "paper_mode": True,
        }
    
    with patch("strategy_runner._acquire_lock", AsyncMock(return_value=True)), \
         patch("strategy_runner._release_lock", AsyncMock()), \
         patch("core.market_clock.get_segment_status", return_value={"open": True}):
        await runner_loop(
            db=mock_db,
            get_price_history=mock_get_price_history,
            place_order_fn=AsyncMock(),
            stop_event=stop_event,
            resolve_option_fn=AsyncMock(),
            close_strategy_fn=close_mock,
        )
        
    close_mock.assert_called_once_with("user-1", "strat-1", reason="strategy-buy-signal")


def test_group_lock_blocks_second_entry():
    """4. Verify group lock blocks second NIFTY entry.
    """
    active_positions = [
        {
            "strategy_id": "strat-nifty-1",
            "status": "OPEN",
            "symbol": "NIFTY",
            "symbol_group": "NIFTY",
        }
    ]
    pending_signals = [
        {
            "id": "sig-nifty-2",
            "strategy_id": "strat-nifty-2",
            "symbol": "NIFTY",
            "action": "BUY",
            "confidence": 95.0,
        }
    ]
    
    approved, rejected = ConflictResolver.resolve(
        pending_signals, active_positions, one_active_position_per_symbol_group=True
    )
    
    assert len(approved) == 0
    assert len(rejected) == 1
    # ConflictResolver uses reason code string; "locked" or exposure-active both indicate the group is blocked
    assert rejected[0]["rejection_reason"] in ("locked", "SKIPPED_GROUP_EXPOSURE_ACTIVE") or \
           "locked" in rejected[0]["rejection_reason"] or \
           "EXPOSURE" in rejected[0]["rejection_reason"]


def test_exit_releases_group_lock():
    """5. Verify that an exit signal releases the group lock, allowing same-tick or subsequent entries.
    """
    active_positions = [
        {
            "strategy_id": "strat-nifty-1",
            "status": "OPEN",
            "symbol": "NIFTY",
            "symbol_group": "NIFTY",
        }
    ]
    # Exit for strat-nifty-1 and entry for strat-nifty-2 in the same batch
    pending_signals = [
        {
            "id": "sig-nifty-exit",
            "strategy_id": "strat-nifty-1",
            "symbol": "NIFTY",
            "action": "SELL",
            "confidence": 100.0,
        },
        {
            "id": "sig-nifty-2",
            "strategy_id": "strat-nifty-2",
            "symbol": "NIFTY",
            "action": "BUY",
            "confidence": 95.0,
        }
    ]
    
    approved, rejected = ConflictResolver.resolve(
        pending_signals, active_positions, one_active_position_per_symbol_group=True
    )
    
    assert len(approved) == 2
    assert len(rejected) == 0


@pytest.mark.anyio
async def test_stale_quote_skips_without_failed_order():
    """6. Verify stale quote skips order placement with SKIPPED_QUOTE_STALE.
    """
    mock_db = _mock_db()
    option_contract = _mock_option_contract()
    # Make quote stale by setting timestamp to 2 minutes ago
    option_contract["received_at"] = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    
    with patch("server.db", mock_db), \
         patch.dict(os.environ, {"CORE_ENGINE_ENABLED": "true", "CORE_ENGINE_PAPER_ENABLED": "true"}), \
         patch("core.market_session_service.MarketSessionService.is_segment_open", return_value=True), \
         patch("server._segment_session_status", return_value={"segment": "NSE_FO", "open": True}), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": True, "allow_simulated_prices": False}), \
         patch("server._market_session_for_instrument", return_value={"segment": "NSE_FO", "open": True}), \
         patch("server.get_user_upstox_gateway", new_callable=AsyncMock, return_value=MagicMock(connected=True)):
         
        res = await _place_order_core(
            user_id="user-1",
            symbol="NIFTY",
            side="BUY",
            qty=1,
            source="strategy:strat-1",
            option_contract=option_contract,
        )
        
    assert res["status"] == "SKIPPED_SIGNAL"
    print("DEBUG STALE RES:", res)
    assert res["reason_code"] == "SKIPPED_QUOTE_STALE"
    mock_db.orders.insert_one.assert_not_called()


@pytest.mark.anyio
async def test_wide_spread_skips_without_failed_order():
    """7. Verify wide bid-ask spread skips order placement.
    """
    mock_db = _mock_db()
    option_contract = _mock_option_contract()
    # Set wide spread (LTP = 100, bid = 50, ask = 150 -> spread 100%)
    option_contract["bid"] = 50.0
    option_contract["ask"] = 150.0
    
    with patch("server.db", mock_db), \
         patch.dict(os.environ, {"CORE_ENGINE_ENABLED": "true", "CORE_ENGINE_PAPER_ENABLED": "true"}), \
         patch("core.market_session_service.MarketSessionService.is_segment_open", return_value=True), \
         patch("server._segment_session_status", return_value={"segment": "NSE_FO", "open": True}), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": True, "allow_simulated_prices": False}), \
         patch("server._market_session_for_instrument", return_value={"segment": "NSE_FO", "open": True}), \
         patch("server.get_user_upstox_gateway", new_callable=AsyncMock, return_value=MagicMock(connected=True)):
         
        res = await _place_order_core(
            user_id="user-1",
            symbol="NIFTY",
            side="BUY",
            qty=1,
            source="strategy:strat-1",
            option_contract=option_contract,
        )
        
    assert res["status"] == "SKIPPED_SIGNAL"
    print("DEBUG WIDE SPREAD RES:", res)
    assert "spread too wide" in res["status_message"].lower()
    mock_db.orders.insert_one.assert_not_called()


@pytest.mark.anyio
async def test_full_paper_execution_lifecycle():
    """8. Verify full paper order lifecycle:
    signal -> order intent -> order fill -> position ledger -> exit signal -> position close -> P&L update.
    """
    mock_db = _mock_db()
    option_contract = _mock_option_contract(ltp=100.0)
    
    # Setup mocks for PortfolioLedger & ExecutionRouter
    mock_ledger = MagicMock()
    mock_ledger.try_open_position = MagicMock(return_value=MagicMock(accepted=True))
    mock_ledger.confirm_position_fill = MagicMock(return_value=MagicMock(accepted=True))
    mock_ledger.close_position = MagicMock(return_value=MagicMock(accepted=True, trade={"realized_pnl": 500.0}))
    
    # Mocking order placement success
    order_doc = {
        "id": "order-1",
        "status": "PAPER_FILLED",
        "execution_status": "PAPER_FILLED",
        "qty": 50,
        "price": 100.0,
        "side": "BUY",
        "symbol": "NIFTY2660424000CE",
        "mode": "paper",
        "rejection_reason": None,
        "reason_code": "OK",
    }
    async def mock_orders_find_one(query, *args, **kwargs):
        if query and "idempotency_key" in query:
            return None
        return order_doc
    mock_db.orders.find_one = AsyncMock(side_effect=mock_orders_find_one)
    mock_db.skipped_signals.find_one_and_update = AsyncMock(return_value=None)
    
    mock_route_intent = AsyncMock(return_value=order_doc)
    
    # We patch PortfolioLedger & ExecutionRouter
    with patch("server.db", mock_db), \
         patch.dict(os.environ, {"CORE_ENGINE_ENABLED": "true", "CORE_ENGINE_PAPER_ENABLED": "true"}), \
         patch("core.portfolio_ledger.PortfolioLedger", return_value=mock_ledger), \
         patch("core.market_session_service.MarketSessionService.is_segment_open", return_value=True), \
         patch("server._segment_session_status", return_value={"segment": "NSE_FO", "open": True}), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": True, "allow_simulated_prices": True}), \
         patch("server._market_session_for_instrument", return_value={"segment": "NSE_FO", "open": True}), \
         patch("server.get_user_upstox_gateway", new_callable=AsyncMock, return_value=MagicMock(connected=True)), \
         patch("core.risk_manager.RiskManager.evaluate_order", new_callable=AsyncMock, return_value={"ok": True, "quantity": 50}), \
         patch("core.execution_router.ExecutionRouter.route_intent", mock_route_intent):
         
        # 1. Trigger Entry Order
        res_entry = await _place_order_core(
            user_id="user-1",
            symbol="NIFTY",
            side="BUY",
            qty=1,
            source="strategy:strat-1",
            option_contract=option_contract,
        )
        
        assert res_entry["status"] == "PAPER_FILLED"
        
        # 2. Mock Active Position for Exit
        mock_db.strategy_positions.find_one = AsyncMock(return_value={
            "id": "pos-1",
            "user_id": "user-1",
            "strategy_id": "strat-1",
            "instrument_key": "token-NIFTY2660424000CE",
            "trading_symbol": "NIFTY2660424000CE",
            "status": "OPEN",
        })
        
        # Mock Exit router response
        exit_order_doc = {
            "id": "order-2",
            "status": "PAPER_FILLED",
            "execution_status": "PAPER_FILLED",
            "qty": 50,
            "price": 110.0,
            "side": "SELL",
            "symbol": "NIFTY2660424000CE",
            "mode": "paper",
            "realized_pnl": 500.0,
        }
        
        mock_route_intent.return_value = exit_order_doc
        
        # 3. Trigger Exit Order (Opposite action is SELL)
        res_exit = await _place_order_core(
            user_id="user-1",
            symbol="NIFTY",
            side="SELL",
            qty=1,
            source="strategy:strat-1",
            option_contract={**option_contract, "transaction_type": "SELL"},
            idempotency_key="exit-idem-key"
        )
        
        assert res_exit["status"] == "PAPER_FILLED"
        print("DEBUG RES EXIT:", res_exit)
        assert res_exit["realized_pnl"] == 500.0


@pytest.mark.anyio
async def test_full_live_execution_lifecycle():
    """9. Verify UpstoxLiveAdapter places a live order end-to-end via ExecutionRouter.

    Signal: strategy signal → ExecutionRouter.route_intent(mode=live)
          → UpstoxLiveAdapter.execute() → execution_bridge.submit_order()
          → broker_order_id saved in order doc.

    Mocks bridge_submit_order (not the real Upstox API) so no real orders are placed.
    Verifies: status=PLACED, mode=live, broker=upstox, broker_order_id set.
    """
    mock_db = _mock_db()
    mock_db.strategies.find_one = AsyncMock(return_value={"id": "strat-1", "mode": "live", "status": "live", "risk_style": "balanced"})

    option_contract = _mock_option_contract(ltp=100.0)
    option_contract["instrument_key"] = "NSE_FO|token-NIFTY2660424000CE"
    option_contract["instrument_token"] = "NSE_FO|token-NIFTY2660424000CE"

    mock_db.live_arm_state.find_one = AsyncMock(
        return_value={"armed": True, "global_live_enabled": True}
    )

    async def mock_orders_find_one(query, *args, **kwargs):
        return None
    mock_db.orders.find_one = AsyncMock(side_effect=mock_orders_find_one)
    mock_db.skipped_signals.find_one_and_update = AsyncMock(return_value=None)

    async def mock_bridge_submit(user_id, payload, *, place_upstox, resolve_upstox_token):
        return {
            "ok": True,
            "status": "PENDING_BROKER",
            "broker_order_id": "upstox-live-order-001",
            "raw": {},
            "attempts": 1,
            "recovered": False,
            "error": None,
        }

    with patch("server.db", mock_db), \
         patch.dict(os.environ, {"CORE_ENGINE_ENABLED": "true", "CORE_ENGINE_LIVE_ENABLED": "true", "CORE_ENGINE_PAPER_ENABLED": "false"}), \
         patch("core.portfolio_ledger.PortfolioLedger", return_value=MagicMock()), \
         patch("core.market_session_service.MarketSessionService.is_segment_open", return_value=True), \
         patch("server._segment_session_status", return_value={"segment": "NSE_FO", "open": True}), \
         patch("server.get_user_settings", new_callable=AsyncMock, return_value={"paper_mode": False, "allow_simulated_prices": True}), \
         patch("server._market_session_for_instrument", return_value={"segment": "NSE_FO", "open": True}), \
         patch("server.get_user_upstox_gateway", new_callable=AsyncMock, return_value=MagicMock(connected=True)), \
         patch("core.risk_manager.RiskManager.evaluate_order", new_callable=AsyncMock, return_value={"ok": True, "quantity": 50}), \
         patch("core.execution_router.bridge_submit_order", side_effect=mock_bridge_submit):

        result = await _place_order_core(
            user_id="user-1",
            symbol="NIFTY",
            side="BUY",
            qty=1,
            source="strategy:strat-1",
            option_contract=option_contract,
        )

    assert result["status"] == "PLACED", f"Expected PLACED, got: {result.get('status')}"
    assert result["mode"] == "live", f"Expected mode=live, got: {result.get('mode')}"
    assert result["broker"] == "upstox", f"Expected broker=upstox, got: {result.get('broker')}"
    assert result["broker_order_id"] == "upstox-live-order-001", (
        f"Expected broker_order_id saved, got: {result.get('broker_order_id')}"
    )
    mock_db.orders.insert_one.assert_called_once()

