"""QuantG Market Domain Definitions.

Establishes strict domain boundaries between NSE, BSE, and MCX segments.
Ensures a strategy is bound to exactly one domain and cannot bleed logic.
"""
from enum import Enum
from typing import Set, Dict, Any

class DomainType(str, Enum):
    NSE_FO = "NSE_FO"
    BSE_FO = "BSE_FO"
    MCX_FO = "MCX_FO"

class MarketDomain:
    def __init__(self, name: DomainType, exchange: str, segment: str, underlyings: Set[str], lot_sizes: Dict[str, int], strike_intervals: Dict[str, int]):
        self.name = name
        self.exchange = exchange
        self.segment = segment
        self.underlyings = underlyings
        self.lot_sizes = lot_sizes
        self.strike_intervals = strike_intervals

    def is_underlying_supported(self, underlying: str) -> bool:
        return str(underlying).upper() in self.underlyings

    def get_lot_size(self, underlying: str) -> int:
        return self.lot_sizes.get(str(underlying).upper(), 1)

    def get_strike_interval(self, underlying: str) -> int:
        return self.strike_intervals.get(str(underlying).upper(), 100)

# Isolated Domain Definitions (SEBI 2026 standardized lot sizes)
NSE_FO_DOMAIN = MarketDomain(
    name=DomainType.NSE_FO,
    exchange="NFO",
    segment="NSE_FO",
    underlyings={"NIFTY", "BANKNIFTY"},
    lot_sizes={"NIFTY": 65, "BANKNIFTY": 30},
    strike_intervals={"NIFTY": 50, "BANKNIFTY": 100}
)

BSE_FO_DOMAIN = MarketDomain(
    name=DomainType.BSE_FO,
    exchange="BFO",
    segment="BSE_FO",
    underlyings={"SENSEX"},
    lot_sizes={"SENSEX": 20},
    strike_intervals={"SENSEX": 100}
)

MCX_FO_DOMAIN = MarketDomain(
    name=DomainType.MCX_FO,
    exchange="MCX",
    segment="MCX_FO",
    underlyings={"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"},
    lot_sizes={"CRUDEOIL": 100, "CRUDEOILM": 10, "NATURALGAS": 1250, "NATGASMINI": 250},
    strike_intervals={"CRUDEOIL": 100, "CRUDEOILM": 100, "NATURALGAS": 5, "NATGASMINI": 5}
)

ALL_DOMAINS = {
    DomainType.NSE_FO: NSE_FO_DOMAIN,
    DomainType.BSE_FO: BSE_FO_DOMAIN,
    DomainType.MCX_FO: MCX_FO_DOMAIN
}

def resolve_domain_by_underlying(underlying: str) -> MarketDomain:
    und_upper = str(underlying).upper()
    if und_upper in NSE_FO_DOMAIN.underlyings:
        return NSE_FO_DOMAIN
    if und_upper in BSE_FO_DOMAIN.underlyings:
        return BSE_FO_DOMAIN
    if und_upper in MCX_FO_DOMAIN.underlyings:
        return MCX_FO_DOMAIN
    
    # Standard equity fallback (e.g. RELIANCE, TCS) to prevent crashes
    return MarketDomain(
        name=DomainType.NSE_FO,
        exchange="NSE",
        segment="NSE_EQ",
        underlyings=set(),
        lot_sizes={},
        strike_intervals={}
    )

def resolve_domain_by_name(name: str) -> MarketDomain:
    try:
        domain_enum = DomainType(str(name).upper())
        return ALL_DOMAINS[domain_enum]
    except (ValueError, KeyError):
        raise ValueError(f"Domain name {name} is unsupported. Must be NSE_FO, BSE_FO, or MCX_FO.")
