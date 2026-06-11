"""QuantG Unified Trading Engine Data Models.

Defines the core data structures for unified paper and live trading:
- Instrument: Resolved tradable instruments with source tracking
- Quote: LTP data with source attribution  
- OrderIntent: Pre-execution order specification
- Position: Position lifecycle tracking
"""

from dataclasses import dataclass, field
from typing import List, Optional
from datetime import date
from enum import Enum
from typing import TypedDict


class InstrumentSource(Enum):
    """Tracks the source of instrument data for debugging and traceability."""
    UPSTOX_MASTER = "UPSTOX_MASTER"          # Real instrument from Upstox master
    PAPER_SIMULATED = "PAPER_SIMULATED"      # Simulated for paper when master unavailable
    UPSTOX_LIVE = "UPSTOX_LIVE"              # Real instrument confirmed for live


@dataclass
class Instrument:
    """Resolved instrument ready for trading.
    
    For NSE/BSE: Index options (NIFTY, BANKNIFTY, SENSEX).
    
    Attributes:
        symbol: Trading symbol (e.g., NIFTY)
        underlying: Base underlying (NIFTY, BANKNIFTY, SENSEX, etc.)
        exchange: Exchange code (NSE, BSE, NFO, BFO)
        segment: Segment (NSE_FO, BSE_FO)
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


# ── TypedDict contracts for MongoDB document shapes ───────────────────────────
# Use total=False so callers only declare fields they guarantee. Agents reading
# these types can know which fields are present without tracing all callers.


class PositionDoc(TypedDict, total=False):
    """Maps to db.strategy_positions.

    Lifecycle: RESERVED → PENDING_OPEN → PENDING_BROKER → OPEN → FILLED → EXITING → CLOSED
    """
    id: str                    # UUID string — primary key
    user_id: str
    strategy_id: str
    symbol: str                # underlying, e.g. "NIFTY"
    target_symbol: str         # traded instrument, e.g. "NIFTY 23200 CE 09 JUN 26"
    trading_symbol: str
    instrument_key: str        # Upstox key, e.g. "NSE_FO|42285"
    instrument_token: str
    exchange: str
    segment: str
    mode: str                  # "paper" | "live"
    position_side: str         # "LONG" | "SHORT"
    quantity: int
    open_quantity: int
    average_buy_price: float
    average_price: float
    last_ltp: float
    ltp_source: str
    unrealized_pnl: float
    realized_pnl: float
    status: str
    tp_sl_tsl_config: dict
    exit_reason: str
    created_at: str            # ISO UTC
    updated_at: str
    closed_at: str


class OrderDoc(TypedDict, total=False):
    """Maps to db.orders."""
    id: str
    user_id: str
    strategy_id: str
    signal_id: str
    symbol: str
    target_symbol: str
    side: str                  # "BUY" | "SELL"
    qty: int
    price: float
    order_type: str            # "MARKET" | "LIMIT"
    status: str
    mode: str
    exchange: str
    idempotency_key: str
    broker_order_id: str
    created_at: str
    updated_at: str


class FillDoc(TypedDict, total=False):
    """Maps to db.trade_fills — immutable audit row per accepted fill."""
    id: str
    fill_id: str               # broker fill ID (unique index on this field)
    user_id: str
    strategy_id: str
    order_id: str
    position_id: str
    symbol: str
    target_symbol: str
    side: str
    quantity: int
    price: float
    mode: str
    action: str                # "OPEN" | "ADD" | "REDUCE" | "CLOSE"
    realized_pnl: float
    exit_reason: str
    created_at: str


class StrategyDoc(TypedDict, total=False):
    """Maps to db.strategies."""
    id: str
    user_id: str
    name: str
    status: str                # "live" | "paused" | "draft"
    mode: str                  # "paper" | "live"
    kind: str                  # strategy classification: "trend" | "range" | "breakout"
    signal_type: str
    visual_config: dict        # full strategy config blob
    today_pnl: float
    total_pnl: float
    total_trades: int
    wins: int
    losses: int
    order_count_today: int
    last_signal_at: str
    created_at: str
    updated_at: str


class SignalEvent(TypedDict, total=False):
    """Normalized signal envelope passed through signal_manager → execution pipeline.

    Maps to db.signals while PENDING; mutated in-place as it moves through the gate.
    """
    id: str
    user_id: str
    strategy_id: str
    action: str                # "BUY" | "SELL"
    symbol: str                # underlying
    target_symbol: str
    mode: str
    status: str                # "PENDING" | "PROCESSED" | "FILTERED" | "SKIPPED_SIGNAL"
    confidence: float
    option_quality_score: float
    option_contract: dict
    visual_config: dict
    idempotency_key: str
    price: float
    ltp: float
    stop_loss: float
    take_profit: float
    rejection_reason: str
    created_at: str
    processed_at: str
