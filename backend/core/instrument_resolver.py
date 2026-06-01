"""QuantG Instrument Resolver.

One source of truth for resolving option and commodity instruments from Zerodha/Upstox masters.
Ensures option instruments are never incorrectly classified as EQUITY.
"""
from typing import Dict, Any, Optional
import logging
from core.market_domains import resolve_domain_by_underlying, DomainType

# Import existing helpers
import options_helper
from mcx_contract_resolver import MCXContractResolver

logger = logging.getLogger("quantg.instrument_resolver")

class InstrumentResolver:
    def __init__(self, db):
        self.db = db
        self.mcx_resolver = MCXContractResolver(db)

    async def resolve_instrument(
        self,
        underlying: str,
        instrument_type: str,          # "INDEX_OPTION" | "MCX_FUTURE" | "MCX_OPTION" | "EQUITY"
        option_side: str = "NONE",      # "CE" | "PE" | "NONE"
        strike_rule: str = "NONE",      # "ATM" | "ITM1" | "OTM1" | "NONE"
        expiry_rule: int = 0,           # expiry offset (0 = current week / near month)
        spot_price_hint: Optional[float] = None,
        kite_client: Optional[Any] = None
    ) -> Optional[Dict[str, Any]]:
        """Resolves active tradable instruments from respective master sources.

        Enforces isolation of BSE_FO, NSE_FO, and MCX_FO.
        """
        und_upper = str(underlying).upper()
        inst_type_upper = str(instrument_type).upper()
        side_upper = str(option_side).upper()
        strike_upper = str(strike_rule).upper()
        
        domain = resolve_domain_by_underlying(und_upper)
        
        # 1. MCX System (MCX_FO)
        if domain.name == DomainType.MCX_FO:
            await self.mcx_resolver.ensure_cache(reason=f"core-resolve:{und_upper}")
            
            if inst_type_upper == "MCX_FUTURE":
                future_row = await self.mcx_resolver.resolve_future(
                    underlying=und_upper,
                    expiry_offset=expiry_rule
                )
                if future_row:
                    return {
                        "tradingsymbol": future_row["trading_symbol"],
                        "trading_symbol": future_row["trading_symbol"],
                        "exchange": "MCX",
                        "segment": "MCX_FO",
                        "instrument_token": future_row["instrument_key"],
                        "instrument_key": future_row["instrument_key"],
                        "asset_class": "MCX_FUTURE",
                        "asset_type": "commodity",
                        "lot_size": int(future_row["lot_size"]),
                        "tick_size": float(future_row.get("tick_size", 0.05)),
                        "expiry": future_row["expiry"]
                    }
                return None
                
            if inst_type_upper == "MCX_OPTION":
                if not spot_price_hint or spot_price_hint <= 0:
                    raise ValueError(f"Spot price hint required for MCX option resolution: {und_upper}")
                
                # Derive strike offset points based on strike rule
                strike_interval = domain.get_strike_interval(und_upper)
                otm_points = 0
                if strike_upper == "OTM1":
                    otm_points = strike_interval
                elif strike_upper == "ITM1":
                    otm_points = -strike_interval  # Opposite of OTM
                
                opt_row = await self.mcx_resolver.resolve(
                    underlying=und_upper,
                    spot=spot_price_hint,
                    option_type=side_upper,
                    strike_interval=strike_interval,
                    otm_points=otm_points,
                    expiry_offset=expiry_rule
                )
                if opt_row:
                    return {
                        "tradingsymbol": opt_row["trading_symbol"],
                        "trading_symbol": opt_row["trading_symbol"],
                        "exchange": "MCX",
                        "segment": "MCX_FO",
                        "instrument_token": opt_row["instrument_token"],
                        "instrument_key": opt_row["instrument_token"],
                        "asset_class": "OPTION_LONG",
                        "asset_type": "option",
                        "option_type": side_upper,
                        "strike": float(opt_row["strike"]),
                        "lot_size": int(opt_row["lot_size"]),
                        "tick_size": 0.05,
                        "expiry": opt_row["expiry"],
                        "underlying": und_upper
                    }
                return None
            
            raise ValueError(f"MCX Domain does not support instrument type: {inst_type_upper}")

        # 2. NSE / BSE Index Options Systems
        if inst_type_upper == "INDEX_OPTION":
            if not kite_client:
                raise ValueError("Kite client instance is required to resolve NSE/BSE Index Option contracts.")
            
            # Map rule to points
            strike_interval = domain.get_strike_interval(und_upper)
            strike_offset = 0
            if strike_upper == "OTM1":
                strike_offset = strike_interval
            elif strike_upper == "ITM1":
                strike_offset = -strike_interval
                
            contract = options_helper.resolve_atm_option(
                kite=kite_client,
                underlying=und_upper,
                option_type=side_upper,
                strike_offset_points=strike_offset,
                expiry_offset_weeks=expiry_rule
            )
            
            if contract:
                return {
                    "tradingsymbol": contract["tradingsymbol"],
                    "trading_symbol": contract["tradingsymbol"],
                    "exchange": contract["exchange"],
                    "segment": domain.segment,
                    "instrument_token": str(contract["instrument_token"]),
                    "instrument_key": f"{contract['exchange']}:{contract['tradingsymbol']}",
                    "asset_class": "OPTION_LONG",
                    "asset_type": "option",
                    "option_type": side_upper,
                    "strike": float(contract["strike"]),
                    "lot_size": int(contract["lot_size"]),
                    "tick_size": 0.05,
                    "expiry": contract["expiry"],
                    "underlying": und_upper
                }
            return None

        raise ValueError(f"Unsupported instrument domain/type combination: {domain.name} / {inst_type_upper}")
