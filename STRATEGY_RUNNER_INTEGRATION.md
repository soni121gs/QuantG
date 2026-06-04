# Strategy Runner Integration Guide

This guide shows how to integrate the unified trading engine into `backend/strategy_runner.py`.

## Current Problem: MCX Strategies Permanently Halt

**Current code halts on contract resolution failure:**
```python
# Old code (strategy_runner.py line ~1200)
if not option_contract:
    await db.strategies.update_one(
        {"id": s["id"]},
        {"$set": {
            "halted": True,
            "halt_reason": "CONTRACT_RESOLUTION_FAILED",  # ← PERMANENT
            "last_error": "contract unresolved"
        }}
    )
    continue
```

**Result**: Strategy is halted forever. Even if MCX master becomes available later or user resets paper, the strategy stays halted.

## Solution: Graceful Skip + Segment-Aware Checks

### Step 1: Add Imports

At top of `backend/strategy_runner.py`, add:

```python
from core.market_session_service import MarketSessionService
from core.market_domains import resolve_domain_by_underlying, DomainType
from core.readiness_checker import ReadinessChecker
from core.position_manager import PositionManager
from core.quote_service import QuoteService
from core.models import InstrumentSource
```

### Step 2: Update Strategy Runner Loop

Replace the old global market check with segment-aware check:

**OLD CODE:**
```python
# Global market check (doesn't work for MCX during NSE close)
from market_protection import is_market_open

for s in strategies:
    if not is_market_open():
        continue  # Skip ALL strategies
```

**NEW CODE:**
```python
# Segment-aware check (allows MCX when NSE closed)
for s in strategies:
    # Get the underlying to determine segment
    underlying = s.get("visual_config", {}).get("option_config", {}).get("underlying", "NIFTY")
    domain = resolve_domain_by_underlying(underlying.upper())
    
    # Check segment-specific market hours
    if not MarketSessionService.is_segment_open(domain):
        logger.info(f"Segment {domain.value} closed, skipping {s['id']}")
        # Update strategy with skip reason (not error)
        await db.strategies.update_one(
            {"id": s["id"]},
            {"$set": {"last_message": f"Segment {domain.value} closed"}}
        )
        continue
```

### Step 3: Replace Option Contract Resolution with Graceful Fallback

**OLD CODE:**
```python
# Option contract resolution
try:
    option_contract = await resolve_option_contract(...)
    if not option_contract:
        # HALT STRATEGY (BAD - permanent)
        await db.strategies.update_one(
            {"id": s["id"]},
            {"$set": {
                "halted": True,
                "halt_reason": "CONTRACT_RESOLUTION_FAILED"
            }}
        )
        continue
except Exception as e:
    await db.strategies.update_one(
        {"id": s["id"]},
        {"$set": {
            "last_error": str(e),
            "halted": True
        }}
    )
    continue
```

**NEW CODE:**
```python
# Use new resolver with source tracking
from core.instrument_resolver import InstrumentResolver

resolver = InstrumentResolver(db)

# Get mode for resolver
mode = "paper" if user.get("paper_mode") else "live"

try:
    # Resolve with source tracking (paper/live aware)
    instrument = await resolver.resolve_instrument_with_source(
        underlying=opt_cfg.get("underlying", "NIFTY"),
        instrument_type=opt_cfg.get("type", "INDEX_OPTION"),
        option_side=opt_cfg.get("side", "CE"),
        strike_rule=opt_cfg.get("strike_mode", "ATM_BUY"),
        expiry_rule=int(opt_cfg.get("expiry_offset") or 0),
        spot_price_hint=spot_price,
        mode=mode  # "paper" or "live"
    )
    
    if not instrument:
        # SKIP GRACEFULLY (not halt)
        # Paper mode: could try simulation fallback
        # Live mode: caught by readiness check
        
        is_mcx = "MCX" in domain.value
        skip_reason = (
            f"MCX master unavailable (paper mode can continue with simulation)"
            if is_mcx and mode == "paper"
            else f"Contract unavailable for {underlying}"
        )
        
        logger.info(f"Strategy {s['id']}: {skip_reason}")
        await db.strategies.update_one(
            {"id": s["id"]},
            {"$set": {"last_message": skip_reason}}
        )
        continue
    
    # Validate source at execution time for live
    if mode == "live" and instrument.source == InstrumentSource.PAPER_SIMULATED:
        logger.error(f"Live trading cannot use paper-simulated instrument: {instrument.symbol}")
        await db.strategies.update_one(
            {"id": s["id"]},
            {"$set": {
                "halted": True,
                "halt_reason": "INVALID_INSTRUMENT_SOURCE",
                "last_error": "Paper-simulated instruments rejected in live mode"
            }}
        )
        continue
    
    option_contract = {
        "tradingsymbol": instrument.symbol,
        "instrument_key": instrument.instrument_key,
        "lot_size": instrument.lot_size,
        # ... other fields
    }

except Exception as e:
    logger.warning(f"Option resolution error for {s['id']}: {e}")
    # Skip on error (don't halt) - might recover next tick
    await db.strategies.update_one(
        {"id": s["id"]},
        {"$set": {"last_message": f"Skipped due to: {str(e)[:100]}"}}
    )
    continue
```

### Step 4: Add Pre-Execution Readiness Check

Add this BEFORE attempting to place an order:

```python
from core.readiness_checker import ReadinessChecker

readiness_checker = ReadinessChecker(db)

# Check if strategy is ready to trade
readiness = await readiness_checker.check_strategy_readiness(
    user_id=s["user_id"],
    strategy_id=s["id"],
    underlying=underlying,
    mode=mode
)

if not readiness["ready"]:
    if readiness.get("can_skip"):
        # Market closed or similar - skip gracefully
        await db.strategies.update_one(
            {"id": s["id"]},
            {"$set": {"last_message": "Skipped: Market segment closed"}}
        )
        continue
    else:
        # Actual readiness failure - halt and log
        reason = readiness.get("checks", {})
        await db.strategies.update_one(
            {"id": s["id"]},
            {"$set": {
                "halted": True,
                "halt_reason": "READINESS_CHECK_FAILED",
                "last_error": str(reason)
            }}
        )
        continue
```

### Step 5: Add Quote Service for Price Fetching

Replace old price fetching with quote service:

**OLD CODE:**
```python
# Old untracked price fetch
ltp = await get_ltp(symbol)  # Source unknown, freshness unknown
```

**NEW CODE:**
```python
from core.quote_service import QuoteService

quote_svc = QuoteService(db, kite_client)

# Get quote with source tracking
quote = await quote_svc.get_quote(
    symbol=underlying,
    mode=mode,  # "paper" or "live"
    allow_simulated=True  # Paper can use simulated
)

if not quote:
    logger.warning(f"Quote unavailable for {underlying}")
    await db.strategies.update_one(
        {"id": s["id"]},
        {"$set": {"last_message": f"No quote available for {underlying}"}}
    )
    continue

spot_price = quote.ltp
logger.debug(f"Got LTP for {underlying}: {spot_price} (source: {quote.source})")
```

### Step 6: Add Position Duplicate Check

Before placing a BUY order:

```python
from core.position_manager import PositionManager

pos_mgr = PositionManager(db)

# Check for duplicate position
has_duplicate = await pos_mgr.check_duplicate_entry(
    user_id=s["user_id"],
    strategy_id=s["id"],
    symbol=underlying,
    side="LONG"  # or "SHORT"
)

if has_duplicate:
    logger.info(f"Duplicate entry blocked: strategy {s['id']} already has active position on {underlying}")
    await db.strategies.update_one(
        {"id": s["id"]},
        {"$set": {"last_message": "Duplicate entry blocked"}}
    )
    continue
```

### Step 7: Track Positions After Trade

After a successful order placement:

```python
# Create position record
position = await pos_mgr.create_position(
    user_id=s["user_id"],
    strategy_id=s["id"],
    symbol=underlying,
    target_symbol=option_contract["tradingsymbol"],
    side="LONG",
    qty=qty,
    entry_price=fill_price,
    mode=mode
)

logger.info(f"Position opened: {position.id}")
```

When closing a position (SELL):

```python
# Find the position to close
active_positions = await pos_mgr.get_active_positions(
    user_id=s["user_id"],
    strategy_id=s["id"],
    symbol=underlying
)

if active_positions:
    position = active_positions[0]
    
    # Execute SELL order
    fill = await place_order(...)
    
    # Close the position
    await pos_mgr.close_position(
        position_id=position.id,
        exit_price=fill_price
    )
    
    logger.info(f"Position closed: {position.id}")
```

## Complete Example Flow

Here's how the integrated strategy runner works:

```python
async def run_strategies_tick():
    """Single tick of strategy runner with unified engine."""
    
    for s in strategies:  # For each strategy
        try:
            # 1. SEGMENT CHECK: Is this market segment open?
            domain = resolve_domain_by_underlying(underlying)
            if not MarketSessionService.is_segment_open(domain):
                logger.info(f"Segment {domain.value} closed, skipping")
                continue
            
            # 2. READINESS CHECK: Is strategy ready?
            readiness = await readiness_checker.check_strategy_readiness(...)
            if not readiness["ready"]:
                logger.info(f"Strategy not ready: {readiness['checks']}")
                continue
            
            # 3. SIGNAL GENERATION: Get strategy signal
            signals = _safe_run(strategy_code, price_data)
            if not signals:
                continue
            
            # 4. QUOTE FETCH: Get fresh price
            quote = await quote_svc.get_quote(underlying, mode=mode)
            if not quote:
                continue
            
            # 5. INSTRUMENT RESOLUTION: Get tradable contract
            instrument = await resolver.resolve_instrument_with_source(...)
            if not instrument:
                continue
            
            # 6. DUPLICATE CHECK: Prevent double trades
            if await pos_mgr.check_duplicate_entry(...):
                continue
            
            # 7. RISK CHECK: All risk rules pass?
            risk_check = await risk_mgr.evaluate_order(...)
            if not risk_check["ok"]:
                continue
            
            # 8. EXECUTE: Place order (paper or live adapter)
            order = await executor.execute(instrument, quote, ...)
            
            # 9. TRACK POSITION: Record position for lifecycle
            position = await pos_mgr.create_position(...)
            
            # Success!
            logger.info(f"Trade placed: {order.id}")
            
        except Exception as e:
            logger.error(f"Unexpected error for {s['id']}: {e}")
            # Skip on error (don't halt) - will retry next tick
```

## Testing the Integration

```bash
# Run acceptance tests to verify integration
cd backend
python -m pytest tests/test_unified_trading_engine.py -v

# Run existing strategy runner tests
python -m pytest tests/test_strategy_leaderboard.py -v
python -m pytest tests/test_signal_manager.py -v
```

## Key Behavioral Changes

### Before (Old Code)

| Scenario | Behavior |
|----------|----------|
| MCX master unavailable | ❌ Strategy halted forever |
| NSE closed + MCX open | ❌ MCX skipped incorrectly |
| Stale quote in live | ⚠️ Used anyway (risky) |
| Live gets PAPER_SIM contract | ✅ Used (BAD - risky) |
| Position tracking | ⚠️ Partial, duplicate positions possible |

### After (New Code)

| Scenario | Behavior |
|----------|----------|
| MCX master unavailable | ✅ Paper continues with simulation, live fails safe |
| NSE closed + MCX open | ✅ MCX runs independently |
| Stale quote in live | ✅ Rejected (>30sec) |
| Live gets PAPER_SIM contract | ❌ Rejected before order |
| Position tracking | ✅ Complete lifecycle, duplicates prevented |

## FAQ

**Q: What if MCX master becomes available later?**
A: Strategy will resume automatically on next tick. No manual intervention needed.

**Q: Will paper mode actually simulate MCX contracts?**
A: Yes. If master unavailable, it uses simulated cache with `source=PAPER_SIMULATED`.

**Q: Why distinguish between skip vs halt?**
A: Skip = temporary (market closed). Halt = permanent (permissions, safety). Paper reset only clears halts, not skips.

**Q: What about backward compatibility?**
A: Fully backward compatible. Old order placement code works unchanged. New services are additive.

**Q: How do I test this locally?**
A: All 24 tests are in `tests/test_unified_trading_engine.py`. Run them before and after integration.

---

**Ready to integrate?** Follow the steps above and run the test suite!
