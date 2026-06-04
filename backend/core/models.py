"""QuantG Unified Trading Engine Data Models.

Defines the core data structures for unified paper and live trading:
- Instrument: Resolved tradable instruments with source tracking
- Quote: LTP data with source attribution  
- OrderIntent: Pre-execution order specification
- Position: Position lifecycle tracking
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import date
from enum import Enum


class InstrumentSource(Enum):
    """Tracks the source of instrument data for debugging and traceability."""
    UPSTOX_MASTER = "UPSTOX_MASTER"          # Real instrument from Upstox master
    PAPER_SIMULATED = "PAPER_SIMULATED"      # Simulated for paper when master unavailable
    UPSTOX_LIVE = "UPSTOX_LIVE"              # Real instrument confirmed for live


@dataclass
class Instrument:
    """Resolved instrument ready for trading.
    
    For NSE/BSE: Index options (NIFTY, BANKNIFTY, SENSEX).
    For MCX: Commodity futures and options.
    
    Attributes:
        symbol: Trading symbol (e.g., NIFTY, CRUDEOILM)
        underlying: Base underlying (NIFTY, NIFTY, CRUDEOILM, etc.)
        exchange: Exchange code (NSE, BSE, MCX)
        segment: Segment (NSE_FO, BSE_FO, MCX_FO)
        instrument_key: Upstox instrument key for API calls
        lot_size: Quantity per contract
        tick_size: Minimum price increment
        expiry: Contract expiry date (futures/options only)
        strike: Strike price (options only)
        option_type: CE/PE (options only)
        source: Instrument data source (master vs. simulated)
    """
    symbol: str
    underlying: str
    exchange: str
    segment: str
    instrument_key: str
    lot_size: int
    tick_size: float
    expiry: Optional[date] = None
    strike: Optional[float] = None
    option_type: Optional[str] = None
    source: InstrumentSource = InstrumentSource.UPSTOX_MASTER


@dataclass
class Quote:
    """Latest quote for an underlying/instrument.
    
    Attributes:
        ltp: Last traded price
        timestamp: ISO format timestamp
        source: Data source (UPSTOX_LIVE, PAPER_SIMULATED, etc.)
        symbol: Trading symbol
    """
    ltp: float
    timestamp: str
    source: str
    symbol: str


@dataclass
class OrderIntent:
    """Pre-execution order specification.
    
    Separates the user's trading intent from actual order execution.
    Enables decoupling of risk checks from adapter-specific logic.
    
    Attributes:
        strategy_id: Strategy placing the order
        symbol: Underlying (e.g., NIFTY, CRUDEOILM)
        target_symbol: Actual traded instrument (e.g., NIFTY26FEB24850CE)
        side: BUY or SELL
        qty: Order quantity
        requested_price: Target execution price
        exchange: Exchange code
        segment: Market segment
        mode: paper or live execution
        idempotency_key: Unique key to prevent duplicate orders
        stop_loss: Optional stop-loss price
        take_profit: Optional take-profit price
    """
    strategy_id: str
    symbol: str
    target_symbol: str
    side: str
    qty: int
    requested_price: float
    exchange: str
    segment: str
    mode: str
    idempotency_key: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass
class Position:
    """Open position state.
    
    Tracks an active position from entry to exit.
    Used for duplicate detection and position reconciliation.
    
    Attributes:
        id: Unique position ID
        user_id: User owning the position
        strategy_id: Strategy that created the position
        symbol: Underlying (e.g., NIFTY, CRUDEOILM)
        target_symbol: Actual traded instrument
        side: LONG or SHORT
        qty: Position quantity
        entry_price: Entry execution price
        entry_time: Entry timestamp (ISO format)
        mode: paper or live
        status: OPEN, CLOSING, CLOSED
    """
    id: str
    user_id: str
    strategy_id: str
    symbol: str
    target_symbol: str
    side: str
    qty: int
    entry_price: float
    entry_time: str
    mode: str
    status: str
    exit_price: Optional[float] = None
    exit_time: Optional[str] = None
