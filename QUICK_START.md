# Quick Start: Unified Trading Engine Integration

## Import All New Components

```python
# Data Models
from core.models import (
    Instrument,
    InstrumentSource,
    Quote,
    OrderIntent,
    Position
)

# Market Sessions
from core.market_session_service import MarketSessionService
from core.market_domains import DomainType

# Services
from core.quote_service import QuoteService
from core.position_manager import PositionManager
from core.readiness_checker import ReadinessChecker
from core.instrument_resolver import InstrumentResolver

# Risk & Execution
from core.risk_manager import RiskManager
from core.execution_router import ExecutionRouter, PaperAdapter, UpstoxLiveAdapter
```

## Typical Strategy Execution Flow

```python
async def execute_strategy(user_id, strategy_id, underlying, mode):
    # 1. Initialize services
    readiness_checker = ReadinessChecker(db)
    instrument_resolver = InstrumentResolver(db)
    risk_manager = RiskManager(db)
    position_manager = PositionManager(db)
    quote_service = QuoteService(db, upstox_client)
    router = ExecutionRouter(db, ledger)
    
    # 2. Check readiness (fails gracefully)
    readiness = await readiness_checker.check_strategy_readiness(
        user_id, strategy_id, underlying, mode
    )
    
    if not readiness["ready"]:
        if readiness.get("can_skip"):
            logger.info("Market closed, skipping tick")
            return
        else:
            logger.error(f"Strategy blocked: {readiness['checks']}")
            # Halt strategy with reason
            return
    
    # 3. Get latest quote with source tracking
    quote = await quote_service.get_quote(underlying, mode=mode)
    if not quote:
        logger.warning(f"No quote available for {underlying}")
        return
    
    # 4. Resolve instrument with source tracking
    instrument = await instrument_resolver.resolve_instrument_with_source(
        underlying=underlying,
        instrument_type="INDEX_OPTION",  # or MCX_OPTION, MCX_FUTURE
        option_side="CE",
        strike_rule="ATM",
        spot_price_hint=quote.ltp,
        kite_client=kite,  # or None for MCX
        mode=mode  # CRITICAL: paper or live
    )
    
    if not instrument:
        logger.error(f"Instrument resolution failed (source={quote.source})")
        if mode == "live":
            # Live mode fails safe: halt strategy
            await halt_strategy(strategy_id, "Instrument resolution failed")
        return
    
    # 5. Check for duplicate entry
    is_duplicate = await position_manager.check_duplicate_entry(
        user_id, strategy_id, underlying, side="LONG"
    )
    if is_duplicate:
        logger.warning("Duplicate entry detected, skipping")
        return
    
    # 6. Perform unified risk checks
    risk_eval = await risk_manager.evaluate_order(
        user_id=user_id,
        strategy_id=strategy_id,
        symbol=underlying,
        target_symbol=instrument.symbol,
        side="BUY",
        requested_qty=qty,
        price=quote.ltp,
        mode=mode  # Identical checks for both modes
    )
    
    if not risk_eval["ok"]:
        logger.warning(f"Order blocked: {risk_eval['reason']}")
        return
    
    # 7. Create order intent (shared between paper and live)
    intent = OrderIntent(
        strategy_id=strategy_id,
        symbol=underlying,
        target_symbol=instrument.symbol,
        side="BUY",
        qty=risk_eval["quantity"],  # Risk-adjusted quantity
        requested_price=quote.ltp,
        exchange=instrument.exchange,
        segment=instrument.segment,
        mode=mode,  # CRITICAL: paper or live
        idempotency_key=str(uuid.uuid4())
    )
    
    # 8. Route to appropriate adapter (only difference between modes)
    try:
        result = await router.route_intent(user_id, intent)
        
        # 9. Create position record on successful fill
        if result["status"] == "FILLED":
            position = await position_manager.create_position(
                user_id=user_id,
                strategy_id=strategy_id,
                symbol=underlying,
                target_symbol=instrument.symbol,
                side="LONG",
                qty=result["qty"],
                entry_price=result["price"],
                mode=mode
            )
            logger.info(f"Position {position.id} created, filled @ {result['price']}")
        
        return result
    except Exception as e:
        logger.error(f"Order execution failed: {e}")
        if mode == "live":
            await halt_strategy(strategy_id, f"Execution failed: {e}")
        return
```

## Segment-Aware Market Sessions

```python
# Check if market is open for specific segment
is_nse_open = MarketSessionService.is_segment_open(DomainType.NSE_FO)
is_mcx_open = MarketSessionService.is_segment_open(DomainType.MCX_FO)

# Get detailed status
status = MarketSessionService.get_segment_status(DomainType.MCX_FO)
print(f"MCX Status: {status['segment']}")
print(f"Open: {status['open']}")
print(f"Hours: {status['trading_hours']}")
print(f"Current IST: {status['current_time_ist']}")

# Get all statuses at once
all_statuses = MarketSessionService.get_all_segment_statuses()
for segment, status in all_statuses.items():
    print(f"{segment}: {'OPEN' if status['open'] else 'CLOSED'}")
```

## Instrument Resolution with Source Tracking

```python
# NSE Option with Master
instrument = await resolver.resolve_instrument_with_source(
    underlying="NIFTY",
    instrument_type="INDEX_OPTION",
    option_side="CE",
    strike_rule="ATM",
    kite_client=kite,
    mode="paper"
)
assert instrument.source == InstrumentSource.UPSTOX_MASTER

# MCX Future (master available)
instrument = await resolver.resolve_instrument_with_source(
    underlying="CRUDEOILM",
    instrument_type="MCX_FUTURE",
    expiry_rule=0,
    mode="paper"
)
assert instrument.source == InstrumentSource.UPSTOX_MASTER

# MCX Option (paper mode - simulates if master unavailable)
instrument = await resolver.resolve_instrument_with_source(
    underlying="CRUDEOILM",
    instrument_type="MCX_OPTION",
    option_side="CE",
    strike_rule="ATM",
    spot_price_hint=400.0,
    mode="paper"  # Paper continues with simulation
)
# May have UPSTOX_MASTER source or PAPER_SIMULATED if master failed

# MCX Option (live mode - fails if master unavailable)
instrument = await resolver.resolve_instrument_with_source(
    underlying="CRUDEOILM",
    instrument_type="MCX_OPTION",
    option_side="CE",
    strike_rule="ATM",
    spot_price_hint=400.0,
    mode="live"  # Live mode fails safe (returns None)
)
# Either UPSTOX_MASTER source or None (no simulation in live)
```

## Readiness Checks

```python
# Check if user is ready for paper trading
paper_ready = await checker.check_paper_readiness(user_id)
if not paper_ready["ready"]:
    print(f"Paper trading not ready: {paper_ready['checks']}")
    # checks = {
    #   "paper_wallet_initialized": bool,
    #   "user_profile_exists": bool
    # }

# Check if user is ready for live trading
live_ready = await checker.check_live_readiness(user_id)
if not live_ready["ready"]:
    print(f"Live trading not ready: {live_ready['checks']}")
    # checks = {
    #   "live_armed": bool,
    #   "upstox_authenticated": bool,
    #   "nse_fo_permission": bool,
    #   "mcx_fo_permission": bool
    # }

# Check if strategy can run
strat_ready = await checker.check_strategy_readiness(
    user_id, strategy_id, "CRUDEOILM", mode="paper"
)
if not strat_ready["ready"]:
    print(f"Strategy not ready: {strat_ready['checks']}")
    # checks = {
    #   "strategy_exists": bool,
    #   "strategy_not_halted": bool,
    #   "segment_open": bool
    # }
    
    if strat_ready.get("can_skip"):
        print("Market closed, skip this tick (don't block)")
        continue
```

## Quote Service with Source Tracking

```python
# Live mode - strict (fails if no fresh quote)
quote = await service.get_quote("NIFTY", mode="live")
if not quote:
    logger.error("Live quote unavailable")
else:
    assert quote.source == "UPSTOX_LIVE"
    print(f"NIFTY LTP: {quote.ltp}")

# Paper mode - lenient (uses simulation as fallback)
quote = await service.get_quote("CRUDEOILM", mode="paper", allow_simulated=True)
if not quote:
    logger.error("No quote available (even simulated)")
else:
    if quote.source == "UPSTOX_LIVE":
        print(f"Using live Upstox quote")
    elif quote.source == "PAPER_SIMULATED":
        print(f"Using cached simulation (master unavailable)")
```

## Position Management

```python
# Create position on entry
position = await pm.create_position(
    user_id=user_id,
    strategy_id=strategy_id,
    symbol="NIFTY",
    target_symbol="NIFTY26FEB24850CE",
    side="LONG",
    qty=1,
    entry_price=450.0,
    mode="paper"
)

# Get active positions
active = await pm.get_active_positions(user_id, strategy_id=strategy_id)
print(f"Active positions: {len(active)}")

# Check for duplicate entry
has_dup = await pm.check_duplicate_entry(
    user_id, strategy_id, "NIFTY", side="LONG"
)
if has_dup:
    print("Already have LONG position in NIFTY")

# Close position on exit
await pm.close_position(position.id, exit_price=460.0)

# Clean up stale locks (stuck positions >1 hour)
cleaned = await pm.clear_stale_locks(user_id, mode="paper")
print(f"Cleaned {cleaned} stale locks")
```

## Risk Manager (Unified Checks)

```python
# Identical checks applied to both paper and live
result = await risk_manager.evaluate_order(
    user_id=user_id,
    strategy_id=strategy_id,
    symbol="NIFTY",
    target_symbol="NIFTY26FEB24850CE",
    side="BUY",
    requested_qty=1,
    price=450.0,
    mode="paper"  # or "live" - same checks
)

if not result["ok"]:
    print(f"Order rejected: {result['reason']}")
    # result["status"] may be:
    # - REJECTED_KILL_SWITCH
    # - REJECTED_ARM_FIREWALL (live only)
    # - REJECTED_STRATEGY_HALTED
    # - REJECTED_MARKET_CLOSED
    # - REJECTED_DAILY_LOSS_LIMIT
    # - REJECTED_RISK_SIZING
else:
    print(f"Order approved for {result['quantity']} units")
```

## Execution Router

```python
# Create router with adapters
router = ExecutionRouter(db, ledger)

# Paper execution (instant, deterministic)
intent = OrderIntent(
    strategy_id="strat_123",
    symbol="NIFTY",
    target_symbol="NIFTY26FEB24850CE",
    side="BUY",
    qty=1,
    requested_price=450.0,
    exchange="NSE",
    segment="NSE_FO",
    mode="paper",  # Key: mode determines adapter
    idempotency_key=str(uuid.uuid4())
)

result = await router.route_intent(user_id, intent)
assert result["mode"] == "paper"
assert result["status"] == "FILLED"  # Paper is instant

# Live execution (async, subject to market)
intent.mode = "live"  # Only difference
result = await router.route_intent(user_id, intent)
assert result["mode"] == "live"
assert result["status"] == "PLACED"  # Live may be pending
```

## Error Handling Patterns

```python
# Graceful handling for missing instruments
try:
    instrument = await resolver.resolve_instrument_with_source(
        underlying="CRUDEOILM",
        instrument_type="MCX_OPTION",
        mode="live"
    )
except Exception as e:
    logger.error(f"Resolution failed: {e}")
    if mode == "live":
        # Live: halt strategy immediately
        await halt_strategy(strategy_id, str(e))
    else:
        # Paper: continue (will use simulation if available)
        pass

# Market session handling
ready = await readiness_checker.check_strategy_readiness(
    user_id, strategy_id, underlying, mode
)

if not ready["ready"]:
    if ready.get("can_skip"):
        # Market closed: skip this tick gracefully
        logger.debug(f"Skipping tick: {ready['checks']}")
    else:
        # Strategy halted: need intervention
        logger.error(f"Strategy needs review: {ready['checks']}")
        send_alert(f"Strategy {strategy_id} blocked")
```

## Configuration

```bash
# Environment variables for tuning
export MCX_OPEN_MINUTE=540        # 09:00 IST
export MCX_CLOSE_MINUTE=1410      # 23:30 IST
export QUOTE_STALE_THRESHOLD_SECONDS=30
export CORE_ENGINE_LIVE_ENABLED=false  # Safety default
```

## Monitoring & Debugging

```python
# Monitor instrument source distribution
async def log_instrument_sources():
    orders = db.orders.find({
        "created_at": {"$gte": yesterday}
    })
    sources = {}
    async for order in orders:
        if "instrument" in order:
            source = order["instrument"].get("source", "unknown")
            sources[source] = sources.get(source, 0) + 1
    
    logger.info(f"Instrument sources: {sources}")
    # Expected: mostly UPSTOX_MASTER, some PAPER_SIMULATED in paper mode

# Monitor readiness failures
async def log_readiness_failures():
    failures = db.readiness_failures.find({
        "created_at": {"$gte": today}
    })
    async for failure in failures:
        logger.info(f"Readiness block: {failure}")
```

---

**Key Takeaway**: Only the `mode` parameter and ReadinessChecker output differs between paper and live. Everything else - risk checks, position tracking, source attribution - is identical. This is the unified pipeline.
