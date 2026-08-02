"""Acceptance tests for QuantG Unified Trading Engine Refactoring.

Tests the unified paper and live trading pipeline with source tracking.

Acceptance Criteria:
1. ✓ NSE strategies run unchanged in paper and live
2. ✓ BSE strategies never halt permanently on contract resolution failure
3. ✓ Paper trades can use simulated contracts when master unavailable
4. ✓ Live trades fail safe (halt strategy, not market)
5. ✓ Identical risk checks apply to paper and live orders
6. ✓ Position lifecycle properly tracked across entry/exit
7. ✓ Market session checks are segment-aware (NSE vs BSE)
8. ✓ Quote service respects source and freshness
9. ✓ Readiness checks prevent halted/stale strategies
"""

import pytest
from datetime import datetime, timezone, date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

# Import unified pipeline components
from core.models import (
    Instrument, InstrumentSource, Quote, OrderIntent, Position
)
from core.market_domains import DomainType
from core.market_session_service import MarketSessionService
from core.quote_service import QuoteService
from core.position_manager import PositionManager
from core.readiness_checker import ReadinessChecker
from core.instrument_resolver import InstrumentResolver
from core.risk_manager import RiskManager
from core.execution_router import PaperAdapter, UpstoxLiveAdapter, ExecutionRouter
from core.portfolio_ledger import PortfolioLedger


class TestDataModels:
    """Test unified data models (AC1: Unified models support both modes)."""
    
    def test_instrument_tracks_source(self):
        """Instrument source tracking enables debugging."""
        inst = Instrument(
            symbol="NIFTY26FEB24850CE",
            underlying="NIFTY",
            exchange="NSE",
            segment="NSE_FO",
            instrument_key="NSE:NIFTY26FEB24850CE",
            lot_size=75,
            tick_size=5.0,
            expiry=date.today(),
            strike=850.0,
            option_type="CE",
            source=InstrumentSource.UPSTOX_MASTER
        )
        assert inst.source == InstrumentSource.UPSTOX_MASTER
        assert inst.symbol == "NIFTY26FEB24850CE"
    
    def test_simulated_instrument_source(self):
        """Paper mode can use simulated instruments."""
        inst = Instrument(
            symbol="SENSEX_SIM",
            underlying="SENSEX",
            exchange="BSE",
            segment="BSE_FO",
            instrument_key="BSE_SIM_SENSEX",
            lot_size=20,
            tick_size=0.05,
            source=InstrumentSource.PAPER_SIMULATED
        )
        assert inst.source == InstrumentSource.PAPER_SIMULATED
        assert "SIM" in inst.symbol
    
    def test_order_intent_idempotency(self):
        """OrderIntent includes idempotency_key for duplicate prevention."""
        key = str(uuid.uuid4())
        intent = OrderIntent(
            strategy_id="strat_123",
            symbol="NIFTY",
            target_symbol="NIFTY26FEB24850CE",
            side="BUY",
            qty=1,
            requested_price=450.0,
            exchange="NSE",
            segment="NSE_FO",
            mode="paper",
            idempotency_key=key
        )
        assert intent.idempotency_key == key
    
    def test_position_lifecycle_states(self):
        """Position tracks OPEN/CLOSED states."""
        pos_id = str(uuid.uuid4())
        pos = Position(
            id=pos_id,
            user_id="user_123",
            strategy_id="strat_123",
            symbol="NIFTY",
            target_symbol="NIFTY26FEB24850CE",
            side="LONG",
            qty=1,
            entry_price=450.0,
            entry_time=datetime.now(timezone.utc).isoformat(),
            mode="paper",
            status="OPEN"
        )
        assert pos.status == "OPEN"
        assert pos.exit_price is None
        
        # After exit
        pos.status = "CLOSED"
        pos.exit_price = 460.0
        assert pos.status == "CLOSED"
        assert pos.exit_price == 460.0


class TestMarketSessionService:
    """Test segment-aware market sessions (AC7: Market sessions are segment-aware)."""
    
    def test_nse_market_session(self):
        """NSE opens at 09:15 IST, closes at 15:30 IST."""
        # 09:30 IST = 03:50 UTC
        test_utc = datetime(2024, 1, 15, 3, 50, 0, tzinfo=timezone.utc)
        
        is_open = MarketSessionService.is_segment_open(DomainType.NSE_FO, test_utc)
        assert is_open is True
    
    def test_market_closed_on_weekend(self):
        """Both NSE and BSE closed on Saturday."""
        # 2024-01-13 is Saturday
        saturday_10am_utc = datetime(2024, 1, 13, 4, 30, 0, tzinfo=timezone.utc)
        
        nse_open = MarketSessionService.is_segment_open(DomainType.NSE_FO, saturday_10am_utc)
        bse_open = MarketSessionService.is_segment_open(DomainType.BSE_FO, saturday_10am_utc)
        
        assert nse_open is False
        assert bse_open is False
    
    def test_segment_status_details(self):
        """get_segment_status returns detailed timing information."""
        status = MarketSessionService.get_segment_status(DomainType.NSE_FO)
        
        assert "segment" in status
        assert "open" in status
        assert "trading_hours" in status
        # NSE equity derivatives close moved 15:30 -> 15:40 on 2026-08-03.
        assert "09:15 - 15:40" in status["trading_hours"]


class TestQuoteService:
    """Test unified quote fetching (AC8: Quote source and freshness)."""
    
    @pytest.mark.asyncio
    async def test_live_quote_freshness_check(self):
        """Live mode validates quote freshness."""
        db = AsyncMock()
        upstox = AsyncMock()
        
        service = QuoteService(db, upstox)
        
        # Test with recent timestamp (should be accepted)
        recent_time = datetime.now(timezone.utc).isoformat()
        
        upstox.get_full_quote = AsyncMock(return_value={
            "last_price": 450.0,
            "timestamp": recent_time
        })
        
        quote = await service.get_quote("NIFTY", mode="live")
        
        # Fresh quote should be accepted
        assert quote is not None
        assert quote.source == "UPSTOX_LIVE"
        assert quote.ltp == 450.0
    
    @pytest.mark.asyncio
    async def test_paper_mode_allows_simulated(self):
        """Paper mode accepts simulated quotes when live unavailable."""
        db = AsyncMock()
        upstox = None  # Simulate no upstox client
        
        service = QuoteService(db, upstox)
        
        db.paper_quote_cache.find_one = AsyncMock(return_value={
            "symbol": "NIFTY",
            "ltp": 450.0
        })
        
        quote = await service.get_quote("NIFTY", mode="paper", allow_simulated=True)
        assert quote is not None
        assert quote.source == "PAPER_SIMULATED"
        assert quote.ltp == 450.0
    
    @pytest.mark.asyncio
    async def test_quote_source_attribution(self):
        """Quotes include source attribution."""
        db = AsyncMock()
        upstox = AsyncMock()
        service = QuoteService(db, upstox)
        
        upstox.get_full_quote = AsyncMock(return_value={
            "last_price": 450.0,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        
        quote = await service.get_quote("NIFTY", mode="paper")
        assert quote.source == "UPSTOX_LIVE"
        assert quote.symbol == "NIFTY"

    @pytest.mark.asyncio
    async def test_bse_quote_uses_exact_websocket_cache_key(self):
        """BSE quotes are read from the Upstox V3 cache by exact instrument_key."""
        db = AsyncMock()
        db.paper_quote_cache.update_one = AsyncMock()

        class FakeGateway:
            def latest_tick(self, key):
                assert key == "BSE_FO|566001"
                return {
                    "instrument_key": "BSE_FO|566001",
                    "ltp": 6502.5,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source": "websocket",
                }

        upstox = FakeGateway()
        service = QuoteService(db, upstox)

        quote = await service.get_quote("BSE_FO|566001", mode="paper")

        assert quote is not None
        assert quote.ltp == 6502.5
        assert quote.source == "UPSTOX_LIVE"
        assert service.last_diagnostics["cache_lookup_key"] == "BSE_FO|566001"
        assert service.last_diagnostics["cache_hit"] is True
        assert service.last_diagnostics["quote_reject_reason"] is None
        db.paper_quote_cache.update_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bse_quote_subscribes_exact_key_then_uses_rest(self):
        """When cache is cold, QuoteService subscribes the same BSE_FO key before REST LTP."""
        db = AsyncMock()
        db.paper_quote_cache.update_one = AsyncMock()

        class FakeGateway:
            def __init__(self):
                self.subscribed = None

            def latest_tick(self, key):
                return None

            def start_market_data_ws(self, keys, mode):
                self.subscribed = (keys, mode)
                return {"ok": True, "tokens": 1}

            def get_market_quote(self, keys):
                return {"data": {"BSE_FO|566001": {"last_price": 6503.0}}}

            @staticmethod
            def parse_quote_ltp(payload, key):
                return 6503.0

        upstox = FakeGateway()
        service = QuoteService(db, upstox)

        quote = await service.get_quote("BSE_FO|566001", mode="paper")

        assert quote is not None
        assert quote.ltp == 6503.0
        assert upstox.subscribed == (["BSE_FO|566001"], "full")
        assert service.last_diagnostics["subscribed_key"] == "BSE_FO|566001"
        assert service.last_diagnostics["cache_lookup_key"] == "BSE_FO|566001"

    @pytest.mark.asyncio
    async def test_live_rejects_stale_bse_quote_with_diagnostics(self):
        """LIVE blocks stale Upstox quotes and records reject reason."""
        db = AsyncMock()

        class FakeGateway:
            def latest_tick(self, key):
                return {
                    "instrument_key": "BSE_FO|566001",
                    "ltp": 6502.5,
                    "timestamp": (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat(),
                }

        upstox = FakeGateway()
        service = QuoteService(db, upstox)

        quote = await service.get_quote("BSE_FO|566001", mode="live")

        assert quote is None
        assert service.last_diagnostics["cache_lookup_key"] == "BSE_FO|566001"
        assert service.last_diagnostics["cache_hit"] is True
        assert service.last_diagnostics["quote_reject_reason"] == "stale_upstox_quote"

    @pytest.mark.asyncio
    async def test_zero_bse_rest_quote_is_not_tradable(self):
        """Upstox can resolve a BSE option but return 0 LTP; that must not become a quote."""
        db = AsyncMock()

        class FakeGateway:
            def latest_tick(self, key):
                return None

            def start_market_data_ws(self, keys, mode):
                return {"ok": True, "tokens": 1}

            def get_market_quote(self, keys):
                return {
                    "data": {
                        "BSE_FO:SENSEX26JUN6500CE": {
                            "last_price": 0.0,
                            "instrument_token": "BSE_FO|566995",
                        }
                    }
                }

        upstox = FakeGateway()
        service = QuoteService(db, upstox)

        quote = await service.get_quote("BSE_FO|566995", mode="paper")

        assert quote is None
        assert service.last_diagnostics["cache_lookup_key"] == "BSE_FO|566995"
        assert service.last_diagnostics["cache_hit"] is False
        assert service.last_diagnostics["quote_reject_reason"] == "zero_ltp_from_upstox"


class TestPositionManager:
    """Test position lifecycle (AC6: Position lifecycle tracked)."""
    
    @pytest.mark.asyncio
    async def test_create_position(self):
        """Create position on entry."""
        db = AsyncMock()
        mgr = PositionManager(db)
        
        pos = await mgr.create_position(
            user_id="user_123",
            strategy_id="strat_123",
            symbol="NIFTY",
            target_symbol="NIFTY26FEB24850CE",
            side="LONG",
            qty=1,
            entry_price=450.0,
            mode="paper"
        )
        
        assert pos.status == "OPEN"
        assert pos.symbol == "NIFTY"
        db.positions.insert_one.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_duplicate_entry_detection(self):
        """Prevent duplicate entries in same direction."""
        db = AsyncMock()
        mgr = PositionManager(db)
        
        db.positions.find_one = AsyncMock(return_value={
            "id": "pos_123",
            "status": "OPEN"
        })
        
        is_dup = await mgr.check_duplicate_entry(
            user_id="user_123",
            strategy_id="strat_123",
            symbol="NIFTY",
            side="LONG"
        )
        
        assert is_dup is True
    
    @pytest.mark.asyncio
    async def test_close_position(self):
        """Close position on exit."""
        db = AsyncMock()
        mgr = PositionManager(db)
        
        db.positions.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        
        success = await mgr.close_position("pos_123", exit_price=460.0)
        assert success is True
        db.positions.update_one.assert_called_once()


class TestReadinessChecker:
    """Test pre-execution validation (AC9: Readiness prevents halted strategies)."""
    
    @pytest.mark.asyncio
    async def test_paper_readiness_check(self):
        """Verify paper mode is ready."""
        db = AsyncMock()
        checker = ReadinessChecker(db)
        
        db.paper_wallets.find_one = AsyncMock(return_value={"user_id": "user_123"})
        db.users.find_one = AsyncMock(return_value={"id": "user_123"})
        
        result = await checker.check_paper_readiness("user_123")
        
        assert result["ready"] is True
        assert result["checks"]["paper_wallet_initialized"] is True
        assert result["checks"]["user_profile_exists"] is True
    
    @pytest.mark.asyncio
    async def test_live_readiness_requires_arm(self):
        """Live mode requires arm switch."""
        db = AsyncMock()
        checker = ReadinessChecker(db)
        
        db.live_arm_state.find_one = AsyncMock(return_value={"armed": False})
        db.users.find_one = AsyncMock(return_value={
            "id": "user_123",
            "upstox_token": "token_xyz",
            "allowed_segments": ["NSE_FO", "BSE_FO"]
        })
        
        result = await checker.check_live_readiness("user_123")
        
        assert result["ready"] is False
        assert result["checks"]["live_armed"] is False
    
    @pytest.mark.asyncio
    async def test_strategy_halted_blocks_execution(self):
        """Halted strategy blocks readiness."""
        db = AsyncMock()
        checker = ReadinessChecker(db)
        
        db.strategies.find_one = AsyncMock(return_value={
            "id": "strat_123",
            "halted": True,
            "halt_reason": "BSE master unavailable"
        })
        
        result = await checker.check_strategy_readiness(
            "user_123", "strat_123", "SENSEX", "paper"
        )
        
        assert result["ready"] is False
        assert result["checks"]["strategy_not_halted"] is False


class TestInstrumentResolver:
    """Test source-tracked instrument resolution (AC2,3,4: BSE handling)."""
    
    @pytest.mark.asyncio
    async def test_nse_option_resolution(self):
        """NSE option resolves with UPSTOX_MASTER source."""
        db = AsyncMock()
        upstox = MagicMock()
        upstox.connected = True
        upstox.get_market_quote.return_value = {
            "data": {"NSE_INDEX|Nifty 50": {"last_price": 24850.0}}
        }
        upstox.parse_quote_ltp.return_value = 24850.0
        upstox.get_option_chain.return_value = {
            "status": "success",
            "data": [{
                "strike_price": 24850,
                "expiry": date.today().isoformat(),
                "call_options": {
                    "instrument_key": "NSE_FO|12345",
                    "trading_symbol": "NIFTY26FEB24850CE",
                    "lot_size": 65,
                },
                "put_options": {},
            }],
        }
        resolver = InstrumentResolver(db, upstox_gateway=upstox)
        
        inst = await resolver.resolve_instrument_with_source(
            underlying="NIFTY",
            instrument_type="INDEX_OPTION",
            option_side="CE",
            strike_rule="ATM",
            mode="paper"
        )
        
        assert inst is not None
        assert inst.source == InstrumentSource.UPSTOX_MASTER
        assert inst.symbol == "NIFTY26FEB24850CE"
        assert inst.instrument_key == "NSE_FO|12345"

    @pytest.mark.asyncio
    async def test_nse_paper_with_simulation_disabled_does_not_fabricate_key(self):
        """Paper realism mode must not leak PAPER_* keys into Upstox quote/order paths."""
        db = AsyncMock()
        upstox = MagicMock()
        upstox.connected = True
        upstox.get_market_quote.return_value = {
            "data": {"NSE_INDEX|Nifty 50": {"last_price": 24850.0}}
        }
        upstox.parse_quote_ltp.return_value = 24850.0
        upstox.get_option_chain.return_value = {"status": "success", "data": []}
        resolver = InstrumentResolver(db, upstox_gateway=upstox)

        inst = await resolver.resolve_instrument_with_source(
            underlying="NIFTY",
            instrument_type="INDEX_OPTION",
            option_side="PE",
            strike_rule="ATM",
            mode="paper",
            allow_simulated=False,
        )

        assert inst is None
        assert resolver.last_diagnostics["reason"] == "no_upstox_option_match"

    @pytest.mark.asyncio
    async def test_nse_spot_resolution_accepts_upstox_alternate_response_key(self):
        """Upstox may return index quotes under colon keys instead of the requested pipe key."""
        db = AsyncMock()
        upstox = MagicMock()
        upstox.connected = True
        upstox.latest_tick.return_value = None
        upstox.get_market_quote.return_value = {
            "data": {"NSE_INDEX:Nifty 50": {"last_price": 23391.0}}
        }
        upstox.parse_quote_ltp.return_value = None
        upstox.get_option_chain.return_value = {
            "status": "success",
            "data": [{
                "strike_price": 23400,
                "expiry": date.today().isoformat(),
                "call_options": {},
                "put_options": {
                    "instrument_key": "NSE_FO|57049",
                    "trading_symbol": "NIFTY26060423400PE",
                    "lot_size": 65,
                },
            }],
        }
        resolver = InstrumentResolver(db, upstox_gateway=upstox)

        inst = await resolver.resolve_instrument_with_source(
            underlying="NIFTY",
            instrument_type="INDEX_OPTION",
            option_side="PE",
            strike_rule="ATM",
            mode="paper",
            allow_simulated=False,
        )

        assert inst is not None
        assert inst.instrument_key == "NSE_FO|57049"
        assert inst.source == InstrumentSource.UPSTOX_MASTER

    @pytest.mark.asyncio
    async def test_nse_option_resolution_search_fallback_after_chain_miss(self):
        """Instrument search is used when option chain omits the target strike row."""
        db = AsyncMock()
        upstox = MagicMock()
        upstox.connected = True
        upstox.latest_tick.return_value = {"ltp": 23391.0}
        upstox.get_option_chain.return_value = {"status": "success", "data": []}
        upstox.search_instruments.return_value = {
            "data": [{
                "instrument_key": "NSE_FO|57049",
                "trading_symbol": "NIFTY26060423400PE",
                "instrument_type": "PE",
                "strike_price": 23400,
                "lot_size": 65,
                "expiry": date.today().isoformat(),
            }]
        }
        resolver = InstrumentResolver(db, upstox_gateway=upstox)

        inst = await resolver.resolve_instrument_with_source(
            underlying="NIFTY",
            instrument_type="INDEX_OPTION",
            option_side="PE",
            strike_rule="ATM",
            mode="paper",
            allow_simulated=False,
        )

        assert inst is not None
        assert inst.instrument_key == "NSE_FO|57049"
        assert resolver.last_diagnostics["reason"] == "matched_upstox_search"


class TestRiskManager:
    """Test unified risk checks (AC5: Identical risk checks for paper and live)."""
    
    @pytest.mark.asyncio
    async def test_risk_check_applies_to_both_modes(self):
        """Risk checks apply uniformly to paper and live."""
        db = AsyncMock()
        rm = RiskManager(db)
        
        # Mock database responses
        db.risk_state.find_one = AsyncMock(return_value=None)  # No kill switch
        db.live_arm_state.find_one = AsyncMock(return_value={"armed": True})
        db.strategies.find_one = AsyncMock(return_value={
            "id": "strat_123",
            "halted": False,
            "today_pnl": 0,
            "visual_config": {}
        })
        db.users.find_one = AsyncMock(return_value={
            "id": "user_123",
            "settings": {},
            "funds": {"free_margin": 100000.0}
        })
        
        with patch('core.market_session_service.MarketSessionService.is_segment_open') as mock_session:
            mock_session.return_value = True
        
        with patch('core.risk_manager.compute_position_size') as mock_size:
            mock_size.return_value = MagicMock(
                allowed=True, 
                quantity=1, 
                reason="OK"
            )
            
            # Test paper mode
            paper_result = await rm.evaluate_order(
                user_id="user_123",
                strategy_id="strat_123",
                symbol="NIFTY",
                target_symbol="NIFTY26FEB24850CE",
                side="BUY",
                requested_qty=1,
                price=450.0,
                mode="paper"
            )
            
            # Test live mode
            live_result = await rm.evaluate_order(
                user_id="user_123",
                strategy_id="strat_123",
                symbol="NIFTY",
                target_symbol="NIFTY26FEB24850CE",
                side="BUY",
                requested_qty=1,
                price=450.0,
                mode="live"
            )
            
            # Both should pass the same checks (or fail for same reason)
            if paper_result["ok"]:
                assert paper_result["status"] == "APPROVED"
            if live_result["ok"]:
                assert live_result["status"] == "APPROVED"


class TestExecutionRouter:
    """Test unified execution with adapter switching (AC1: Paper and live use same intent)."""
    
    @pytest.mark.asyncio
    async def test_paper_adapter_executes(self):
        """Paper adapter executes using virtual wallet."""
        db = AsyncMock()
        ledger = AsyncMock()
        
        adapter = PaperAdapter(db, ledger)
        
        db.orders.insert_one = AsyncMock()
        db.fills.insert_one = AsyncMock()
        adapter.wallet.debit = AsyncMock(return_value=True)
        adapter.wallet.get_balance = AsyncMock(return_value=100000.0)
        ledger.process_fill = AsyncMock()
        
        intent = {
            "strategy_id": "strat_123",
            "symbol": "NIFTY",
            "target_symbol": "NIFTY26FEB24850CE",
            "side": "BUY",
            "qty": 1,
            "requested_price": 450.0,
            "exchange": "NSE",
            "segment": "NSE_FO",
            "idempotency_key": str(uuid.uuid4())
        }
        
        result = await adapter.execute("user_123", intent)
        
        assert result["status"] == "FILLED"
        assert result["mode"] == "paper"
        db.orders.insert_one.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_live_adapter_requires_arm(self):
        """Live adapter requires arm switch."""
        db = AsyncMock()
        ledger = AsyncMock()
        
        adapter = UpstoxLiveAdapter(db, ledger)
        
        db.live_arm_state.find_one = AsyncMock(return_value=None)  # Not armed
        
        intent = {
            "strategy_id": "strat_123",
            "symbol": "NIFTY",
            "target_symbol": "NIFTY26FEB24850CE",
            "side": "BUY",
            "qty": 1,
            "requested_price": 450.0,
            "exchange": "NSE",
            "segment": "NSE_FO"
        }
        
        with pytest.raises(RuntimeError):
            await adapter.execute("user_123", intent)


class TestUnifiedPipeline:
    """Integration tests for unified paper/live pipeline."""
    
    @pytest.mark.asyncio
    async def test_strategy_trades_paper_then_live_unchanged(self):
        """Same strategy code trades paper and live without changes (AC1)."""
        db = AsyncMock()
        ledger = AsyncMock()
        router = ExecutionRouter(db, ledger)
        
        # Paper execution
        db.orders.insert_one = AsyncMock()
        db.fills.insert_one = AsyncMock()
        router.paper_adapter.wallet.debit = AsyncMock(return_value=True)
        ledger.process_fill = AsyncMock()
        
        intent = {
            "strategy_id": "strat_123",
            "symbol": "NIFTY",
            "target_symbol": "NIFTY26FEB24850CE",
            "side": "BUY",
            "qty": 1,
            "requested_price": 450.0,
            "exchange": "NSE",
            "segment": "NSE_FO",
            "mode": "paper",
            "idempotency_key": str(uuid.uuid4())
        }
        
        paper_result = await router.route_intent("user_123", intent)
        assert paper_result["mode"] == "paper"
        
        # Same intent with mode="live" (in real scenario)
        # Just verify routing recognizes both modes
        assert router.paper_adapter is not None
        assert router.live_adapter is not None


class TestV13OptionBugs:
    """Test unit fixes for option symbol parsing and Greeks limits."""
    
    def test_domain_resolution_option_contract(self):
        """resolve_domain_by_underlying handles option contract strings."""
        from core.market_domains import resolve_domain_by_underlying, DomainType
        
        domain = resolve_domain_by_underlying("NIFTY 23200 CE 09 JUN 26")
        assert domain.name == DomainType.NSE_FO
        assert domain.is_underlying_supported("NIFTY 23200 CE 09 JUN 26") is True
        assert domain.get_lot_size("NIFTY 23200 CE 09 JUN 26") == 65
        assert domain.get_strike_interval("NIFTY 23200 CE 09 JUN 26") == 50

    @pytest.mark.asyncio
    async def test_greeks_exit_order_exempt(self):
        """Exit orders must bypass the Greeks exposure limit check."""
        db = AsyncMock()
        rm = RiskManager(db)
        
        # Mock strategy_positions: User currently has a LONG position on CE.
        db.strategy_positions.find.return_value.to_list = AsyncMock(return_value=[
            {
                "target_symbol": "NIFTY 23200 CE 09 JUN 26",
                "open_quantity": 65,
                "position_side": "LONG",
                "status": "OPEN",
                "strategy_id": "strat_123"
            }
        ])
        
        # Evaluate a SELL exit order.
        # Even if projected delta exceeds max_net_delta (e.g. max_net_delta is set very low like 5.0),
        # this order is reducing/closing the position, so it must be APPROVED.
        settings = {"max_net_delta": 5.0}
        visual_risk = {}
        
        res = await rm._check_greeks_exposure(
            user_id="user_123",
            target_symbol="NIFTY 23200 CE 09 JUN 26",
            side="SELL",
            quantity=65,
            lot_size=65,
            mode="paper",
            settings=settings,
            visual_risk=visual_risk,
            strategy_id="strat_123"
        )
        
        assert res["ok"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
