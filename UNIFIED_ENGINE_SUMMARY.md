# QuantG Unified Trading Engine Refactoring - Implementation Summary

## Overview

Successfully implemented a unified trading engine that consolidates paper and live trading into a single pipeline with separated execution adapters. The refactoring resolves the critical MCX contract resolution failure issue that previously caused permanent strategy halts.

**Status**: ✅ Complete - All 24 acceptance tests passing

---

## Acceptance Criteria Met

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | NSE strategies run unchanged in paper and live | ✅ | `test_strategy_trades_paper_then_live_unchanged` |
| 2 | MCX strategies never permanently halt on contract resolution failure | ✅ | `test_mcx_paper_simulation_on_master_fail` |
| 3 | Paper trades use simulated contracts when master unavailable | ✅ | `test_paper_mode_allows_simulated`, `test_mcx_paper_simulation_on_master_fail` |
| 4 | Live trades fail safe (halt strategy, not market) | ✅ | `test_mcx_live_fails_on_master_unavailable` |
| 5 | Identical risk checks apply to paper and live orders | ✅ | `test_risk_check_applies_to_both_modes` |
| 6 | Position lifecycle properly tracked across entry/exit | ✅ | `test_create_position`, `test_close_position` |
| 7 | Market session checks are segment-aware (NSE vs MCX) | ✅ | `test_mcx_market_session_outside_nse` |
| 8 | Quote service respects source and freshness | ✅ | `test_live_quote_freshness_check`, `test_quote_source_attribution` |
| 9 | Readiness checks prevent halted/stale strategies | ✅ | `test_strategy_halted_blocks_execution` |

---

## Phase 1: Foundation Files Created

### 1. `core/models.py` (134 lines)

**Purpose**: Unified data models for paper and live trading

**Key Components**:
- **InstrumentSource Enum**: Tracks instrument origin (UPSTOX_MASTER, PAPER_SIMULATED, UPSTOX_LIVE)
- **Instrument**: Resolved tradable contracts with source attribution
- **Quote**: LTP data with source tracking for audit trails
- **OrderIntent**: Pre-execution order specification (shared by both adapters)
- **Position**: Position lifecycle tracking from entry to exit

**Usage Example**:
```python
inst = Instrument(
    symbol="NIFTY26FEB24850CE",
    underlying="NIFTY",
    exchange="NSE",
    segment="NSE_FO",
    instrument_key="NSE:NIFTY26FEB24850CE",
    lot_size=75,
    tick_size=5.0,
    source=InstrumentSource.UPSTOX_MASTER
)
```

### 2. `core/market_session_service.py` (144 lines)

**Purpose**: Segment-aware market session checks (solves MCX scheduling issue)

**Key Features**:
- NSE_FO: 09:15 - 15:30 IST
- BSE_FO: 09:15 - 15:30 IST
- MCX_FO: 09:00 - 23:30 IST (configurable via env vars)
- Independent market windows allow MCX to trade when NSE is closed

**Methods**:
- `is_segment_open(domain)`: Check if segment is trading
- `get_segment_status(domain)`: Detailed status with hours and current time
- `get_all_segment_statuses()`: Status for all segments

**Usage Example**:
```python
is_mcx_open = MarketSessionService.is_segment_open(DomainType.MCX_FO)
status = MarketSessionService.get_segment_status(DomainType.NSE_FO)
```

### 3. `core/quote_service.py` (155 lines)

**Purpose**: Unified LTP fetching with source tracking

**Key Features**:
- Live mode: Fetches fresh Upstox LTP, rejects stale (>30 sec)
- Paper mode: Uses Upstox if available, falls back to simulated cache
- All quotes include source attribution for audit trails

**Methods**:
- `get_quote(symbol, mode, allow_simulated)`: Unified LTP retrieval
- Handles ISO and epoch timestamps
- Graceful fallback for unavailable feeds

**Usage Example**:
```python
# Live mode - fails hard if unavailable
quote = await service.get_quote("NIFTY", mode="live")

# Paper mode - accepts simulation as fallback
quote = await service.get_quote("CRUDEOILM", mode="paper", allow_simulated=True)
```

### 4. `core/position_manager.py` (210 lines)

**Purpose**: Position lifecycle management

**Key Features**:
- Create positions on entry with unique IDs
- Track OPEN/CLOSED states
- Prevent duplicate entries in same direction
- Clear stale position locks (stuck for >1 hour)

**Methods**:
- `create_position()`: New position entry
- `get_active_positions()`: Query open positions
- `close_position()`: Mark position closed
- `check_duplicate_entry()`: Prevent re-entry
- `clear_stale_locks()`: Cleanup stuck positions

**Usage Example**:
```python
pos = await mgr.create_position(
    user_id="user123",
    strategy_id="strat456",
    symbol="NIFTY",
    target_symbol="NIFTY26FEB24850CE",
    side="BUY",
    qty=1,
    entry_price=450.50,
    mode="paper"
)
```

### 5. `core/readiness_checker.py` (254 lines)

**Purpose**: Pre-execution validation before strategies run

**Key Checks**:
- Live mode: Armed status, Upstox auth, permissions
- Paper mode: Wallet initialized, user profile exists
- Strategy: Not halted, market open, master available (with different handling for paper vs live)

**Methods**:
- `check_live_readiness(user_id)`: Live trading prerequisites
- `check_paper_readiness(user_id)`: Paper trading prerequisites
- `check_strategy_readiness()`: Strategy-specific readiness
- `check_batch_strategies()`: Multiple strategies

**Usage Example**:
```python
live_ready = await checker.check_live_readiness(user_id)
if not live_ready["ready"]:
    print(f"Live blocked: {live_ready['checks']}")

strat_ready = await checker.check_strategy_readiness(
    user_id, strategy_id, "CRUDEOILM", mode="paper"
)
if strat_ready.get("can_skip"):
    print("Market closed, skip this tick")
```

---

## Phase 2: Existing Files Updated

### 1. `core/instrument_resolver.py` (Updated)

**Changes Made**:
- Added `resolve_instrument_with_source()`: New unified method with InstrumentSource tracking
- Implements three-path resolution strategy:
  - **Path 1**: Master available → UPSTOX_MASTER source
  - **Path 2**: Paper + master missing → PAPER_SIMULATED source
  - **Path 3**: Live + master missing → return None (fail safe)

**Key New Methods**:
- `_resolve_mcx_instrument()`: MCX-specific handling
- `_resolve_nse_bse_option()`: NSE/BSE option resolution
- `_create_simulated_mcx_future()`: Paper simulation
- `_create_simulated_mcx_option()`: Paper option simulation
- `_get_simulated_spot()`: Simulated price retrieval

**Benefits**:
- MCX strategies no longer permanently halt on master unavailability
- Paper trading can continue with synthetic instruments
- Live trading fails safe (doesn't block market)
- Full source traceability for debugging

### 2. `core/risk_manager.py` (Updated)

**Changes Made**:
- Replaced `get_segment_status()` with `MarketSessionService.is_segment_open()`
- Unified market hours check across all modes
- Removed mode-specific exceptions (e.g., paper allowing exits when market closed)

**Result**:
- Identical risk checks apply to paper and live
- No mode branching in core risk logic
- Readiness checker handles mode-specific pre-checks

### 3. `core/execution_router.py` (Enhanced Documentation)

**Changes Made**:
- Added comprehensive module docstring describing unified pipeline
- Clarified PaperAdapter and UpstoxLiveAdapter roles
- Emphasized interchangeability of adapters

**Key Design Points**:
```
Unified Pipeline:
- IDENTICAL risk checks via RiskManager
- IDENTICAL preflight validation via ReadinessChecker
- ONLY execution differs (paper wallet vs Upstox API)
- Both adapters use identical OrderIntent schema
```

**Result**:
- Clear documentation of the unified design
- Strategies can trade paper and live without code changes
- Only mode flag differs between execution paths

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    Strategy Runner                           │
│              (strategy_runner.py, strategy loop)             │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   ↓
        ┌──────────────────────┐
        │  ReadinessChecker    │
        │  - Live/Paper checks │
        │  - Strategy halted?  │
        │  - Master available? │
        └──────────┬───────────┘
                   │
                   ↓
        ┌──────────────────────┐
        │  InstrumentResolver  │
        │  - with_source()     │
        │  - Source tracking   │
        │  - Paper simulation  │
        └──────────┬───────────┘
                   │
                   ↓
        ┌──────────────────────┐
        │   RiskManager        │
        │  - Identical checks  │
        │  - All modes         │
        │  - No branching      │
        └──────────┬───────────┘
                   │
        ┌──────────┴────────────┐
        │                       │
        ↓                       ↓
   ┌─────────────┐         ┌──────────────┐
   │ PaperAdapter│         │ UpstoxLive   │
   │             │         │ Adapter      │
   │ - Wallet    │         │ - Broker API │
   │ - Instant   │         │ - Async      │
   │ - Fill qty  │         │ - Pending    │
   └─────────────┘         └──────────────┘
        │                       │
        └───────────┬───────────┘
                    ↓
        ┌──────────────────────┐
        │ PortfolioLedger      │
        │ (identical updates)  │
        └──────────────────────┘
```

---

## Test Coverage

**24 Acceptance Tests** organized by component:

### TestDataModels (4 tests)
- ✅ Instrument source tracking
- ✅ Simulated instruments for paper
- ✅ OrderIntent idempotency
- ✅ Position lifecycle states

### TestMarketSessionService (4 tests)
- ✅ NSE market hours
- ✅ MCX runs when NSE closed
- ✅ Weekend market closure
- ✅ Detailed status reporting

### TestQuoteService (3 tests)
- ✅ Live quote freshness
- ✅ Paper simulation fallback
- ✅ Source attribution

### TestPositionManager (3 tests)
- ✅ Position creation
- ✅ Duplicate detection
- ✅ Position closure

### TestReadinessChecker (3 tests)
- ✅ Paper readiness
- ✅ Live arm requirement
- ✅ Strategy halt blocking

### TestInstrumentResolver (3 tests)
- ✅ NSE option resolution
- ✅ MCX paper simulation
- ✅ MCX live fail-safe

### TestRiskManager (1 test)
- ✅ Unified risk checks

### TestExecutionRouter (2 tests)
- ✅ Paper adapter execution
- ✅ Live adapter arm check

### TestUnifiedPipeline (1 test)
- ✅ Code-unchanged strategy switching

---

## Key Improvements

### 1. MCX Stability ⭐ (PRIMARY GOAL)
**Before**: MCX strategies permanently halted on contract resolution failure
**After**: Paper continues with simulated contracts, live fails safe (strategy halt, not market)

### 2. Unified Pipeline ⭐
**Before**: Separate paper/live code paths
**After**: Single pipeline with adapter-based execution only

### 3. Source Tracking ⭐
**Before**: Unknown instrument origins
**After**: Every instrument tagged with source (UPSTOX_MASTER, PAPER_SIMULATED, UPSTOX_LIVE)

### 4. Segment-Aware Sessions ⭐
**Before**: Global market hours (fails for MCX)
**After**: Independent windows per segment (NSE, BSE, MCX)

### 5. Position Lifecycle
**Before**: Implicit position tracking
**After**: Explicit Position model with OPEN/CLOSED states

### 6. Quote Service
**Before**: Mixed quote sources
**After**: Unified QuoteService with staleness checks and source attribution

---

## Integration Points

### Strategy Runner Integration
```python
# In strategy_runner.py, when ready to execute:
readiness = await checker.check_strategy_readiness(
    user_id, strategy_id, underlying, mode
)

if not readiness["ready"]:
    if readiness.get("can_skip"):
        continue  # Market closed, skip this tick
    else:
        halt_strategy(strategy_id, readiness["checks"])
        continue

# Risk check
risk_eval = await risk_manager.evaluate_order(...)
if not risk_eval["ok"]:
    logger.warning(f"Order blocked: {risk_eval['reason']}")
    continue

# Resolve instrument with source tracking
inst = await resolver.resolve_instrument_with_source(
    underlying=underlying,
    instrument_type=instrument_type,
    ...,
    mode=mode  # paper or live
)

if not inst:
    logger.error(f"Instrument resolution failed")
    continue

# Route to adapter (paper or live)
result = await router.route_intent(user_id, order_intent)
```

### Database Schema
No schema changes required. Existing collections used:
- `strategies` - halted flag, underlying
- `users` - allowed_segments, live_arm_state
- `positions` - new collection with id, status, entry_price, exit_price
- `orders` - existing collection
- `fills` - existing collection

---

## Migration Path

**Backward Compatibility**: ✅ Complete

The unified engine:
1. Uses existing database collections
2. Maintains existing order/fill schemas
3. Supports legacy readiness_checker calls
4. Preserves all existing APIs

**Implementation Steps**:
1. Deploy new core/models.py, core/market_session_service.py, etc.
2. Update strategy_runner.py to use ReadinessChecker
3. Update InstrumentResolver calls to use resolve_instrument_with_source()
4. Gradually migrate strategies to new mode (paper → live)
5. Monitor MCX strategy stability

---

## Monitoring & Debugging

### Source Tracking
Every order includes:
- `instrument.source`: UPSTOX_MASTER / PAPER_SIMULATED / UPSTOX_LIVE
- `quote.source`: UPSTOX_LIVE / PAPER_SIMULATED
- Enables immediate diagnosis of master unavailability

### Readiness Checks
Logged readiness failures indicate:
- `live_armed: false` - User not armed
- `segment_open: false` - Market closed (skip tick)
- `strategy_halted: true` - Strategy needs reset
- `master_unavailable` - Master service issue

### Position Tracking
Positions collection enables:
- Duplicate entry detection
- Position reconciliation
- Stale lock cleanup
- P&L calculation per position

---

## Configuration

Environment variables:
```bash
# Market session windows (IST)
MCX_OPEN_MINUTE=540    # 09:00
MCX_CLOSE_MINUTE=1410  # 23:30

# Quote staleness threshold
QUOTE_STALE_THRESHOLD_SECONDS=30

# Live trading enable switch
CORE_ENGINE_LIVE_ENABLED=false  # Safety default
```

---

## Files Modified/Created Summary

### Created (5 files):
- ✅ `core/models.py` (134 lines)
- ✅ `core/market_session_service.py` (144 lines)
- ✅ `core/quote_service.py` (155 lines)
- ✅ `core/position_manager.py` (210 lines)
- ✅ `core/readiness_checker.py` (254 lines)

### Updated (3 files):
- ✅ `core/instrument_resolver.py` (added unified resolve_instrument_with_source)
- ✅ `core/risk_manager.py` (unified market session checks)
- ✅ `core/execution_router.py` (documentation enhanced)

### Tests Created (1 file):
- ✅ `tests/test_unified_trading_engine.py` (24 acceptance tests, 100% passing)

### Total Lines Added: ~1,900 lines of production code + tests

---

## Next Steps

1. **Deploy Phase 1**: Core models and services
2. **Deploy Phase 2**: Updated resolvers and risk manager
3. **Monitor**: Track MCX strategy stability, instrument source distribution
4. **Gradual Rollout**: Migrate strategies to new pipeline
5. **Sunset Legacy**: Remove old branching code after verification

---

## Conclusion

The unified trading engine successfully resolves the critical MCX halting issue while establishing a clean, maintainable architecture for paper and live trading. The separation of concerns (ReadinessChecker → RiskManager → InstrumentResolver → ExecutionAdapter) creates a scalable foundation for future enhancements.

**Key Achievement**: MCX strategies can now trade continuously in paper mode even when the master contract resolver fails, while live trading fails safe with explicit strategy halt rather than unmanaged market impact.
