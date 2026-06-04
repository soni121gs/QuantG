"""QuantG Instrument Resolver.

One source of truth for resolving option and commodity instruments from Zerodha/Upstox masters.
Ensures option instruments are never incorrectly classified as EQUITY.

Unified paper/live pipeline:
- Master available → UPSTOX_MASTER source
- Paper + master missing → PAPER_SIMULATED source (with fallback)
- Live + master missing → return None (fail in readiness check)
"""
from typing import Dict, Any, Optional
import logging
from core.market_domains import resolve_domain_by_underlying, DomainType
from core.models import Instrument, InstrumentSource

# Import existing helpers
import options_helper
from mcx_contract_resolver import MCXContractResolver

logger = logging.getLogger("quantg.instrument_resolver")

class InstrumentResolver:
    def __init__(self, db):
        self.db = db
        self.mcx_resolver = MCXContractResolver(db)

    async def resolve_instrument_with_source(
        self,
        underlying: str,
        instrument_type: str,
        option_side: str = "NONE",
        strike_rule: str = "NONE",
        expiry_rule: int = 0,
        spot_price_hint: Optional[float] = None,
        kite_client: Optional[Any] = None,
        mode: str = "paper"
    ) -> Optional[Instrument]:
        """Resolve instrument with source tracking (unified paper/live pipeline).
        
        Three paths:
        1. Master available → InstrumentSource.UPSTOX_MASTER
        2. Paper + master missing → InstrumentSource.PAPER_SIMULATED
        3. Live + master missing → return None (fail in readiness check)
        
        Args:
            underlying: Underlying symbol (e.g., NIFTY, CRUDEOILM)
            instrument_type: INDEX_OPTION, MCX_FUTURE, MCX_OPTION, EQUITY
            option_side: CE, PE, NONE
            strike_rule: ATM, ITM1, OTM1, NONE
            expiry_rule: Expiry offset
            spot_price_hint: Spot price for MCX option strike selection
            kite_client: Kite/Upstox client for NSE/BSE resolution
            mode: "paper" or "live"
            
        Returns:
            Instrument with source tracking, or None if unresolvable
        """
        und_upper = str(underlying).upper()
        inst_type_upper = str(instrument_type).upper()
        side_upper = str(option_side).upper()
        strike_upper = str(strike_rule).upper()
        
        domain = resolve_domain_by_underlying(und_upper)
        
        # MCX System (MCX_FO)
        if domain.name == DomainType.MCX_FO:
            return await self._resolve_mcx_instrument(
                und_upper, inst_type_upper, side_upper, strike_upper,
                expiry_rule, spot_price_hint, mode
            )
        
        # NSE / BSE Index Options
        if inst_type_upper == "INDEX_OPTION":
            return await self._resolve_nse_bse_option(
                und_upper, inst_type_upper, side_upper, strike_upper,
                expiry_rule, kite_client, domain
            )
        
        logger.error(f"Unsupported instrument: {domain.name} / {inst_type_upper}")
        return None
    
    async def _resolve_mcx_instrument(
        self,
        underlying: str,
        inst_type: str,
        option_side: str,
        strike_rule: str,
        expiry_rule: int,
        spot_price_hint: Optional[float],
        mode: str
    ) -> Optional[Instrument]:
        """Resolve MCX instrument with paper/live split."""
        try:
            await self.mcx_resolver.ensure_cache(reason=f"core-resolve:{underlying}")
        except Exception as e:
            logger.warning(f"MCX master cache load failed: {e}")
            if mode == "live":
                # Live mode: hard fail if master unavailable
                logger.error(f"Live mode requires MCX master, unavailable for {underlying}")
                return None
            # Paper mode: continue to try simulation
        
        # Try to resolve from master
        if inst_type == "MCX_FUTURE":
            future_row = await self.mcx_resolver.resolve_future(
                underlying=underlying,
                expiry_offset=expiry_rule
            )
            if future_row:
                return Instrument(
                    symbol=future_row["trading_symbol"],
                    underlying=underlying,
                    exchange="MCX",
                    segment="MCX_FO",
                    instrument_key=future_row["instrument_key"],
                    lot_size=int(future_row["lot_size"]),
                    tick_size=float(future_row.get("tick_size", 0.05)),
                    expiry=future_row.get("expiry"),
                    source=InstrumentSource.UPSTOX_MASTER
                )
            
            # Master missing: paper can simulate, live fails
            if mode == "paper":
                sim = await self._create_simulated_mcx_future(underlying, expiry_rule)
                if sim:
                    return sim
            return None
        
        if inst_type == "MCX_OPTION":
            if not spot_price_hint or spot_price_hint <= 0:
                if mode == "live":
                    logger.error(f"Spot price required for live MCX option: {underlying}")
                    return None
                # Paper: try to continue with simulated price
                spot_price_hint = await self._get_simulated_spot(underlying)
                if not spot_price_hint:
                    return None
            
            domain = resolve_domain_by_underlying(underlying)
            strike_interval = domain.get_strike_interval(underlying)
            otm_points = 0
            if strike_rule == "OTM1":
                otm_points = strike_interval
            elif strike_rule == "ITM1":
                otm_points = -strike_interval
            
            # Try to resolve from master
            opt_row = await self.mcx_resolver.resolve(
                underlying=underlying,
                spot=spot_price_hint,
                option_type=option_side,
                strike_interval=strike_interval,
                otm_points=otm_points,
                expiry_offset=expiry_rule
            )
            if opt_row:
                return Instrument(
                    symbol=opt_row["trading_symbol"],
                    underlying=underlying,
                    exchange="MCX",
                    segment="MCX_FO",
                    instrument_key=opt_row["instrument_token"],
                    lot_size=int(opt_row["lot_size"]),
                    tick_size=0.05,
                    expiry=opt_row.get("expiry"),
                    strike=float(opt_row["strike"]),
                    option_type=option_side,
                    source=InstrumentSource.UPSTOX_MASTER
                )
            
            # Master missing: paper can simulate, live fails
            if mode == "paper":
                sim = await self._create_simulated_mcx_option(
                    underlying, option_side, spot_price_hint, strike_rule
                )
                if sim:
                    return sim
            return None
        
        logger.error(f"MCX Domain does not support: {inst_type}")
        return None
    
    async def _resolve_nse_bse_option(
        self,
        underlying: str,
        inst_type: str,
        option_side: str,
        strike_rule: str,
        expiry_rule: int,
        kite_client: Optional[Any],
        domain
    ) -> Optional[Instrument]:
        """Resolve NSE/BSE option with source tracking."""
        if not kite_client:
            logger.error("Kite client required for NSE/BSE option resolution")
            return None
        
        strike_interval = domain.get_strike_interval(underlying)
        strike_offset = 0
        if strike_rule == "OTM1":
            strike_offset = strike_interval
        elif strike_rule == "ITM1":
            strike_offset = -strike_interval
        
        contract = options_helper.resolve_atm_option(
            kite=kite_client,
            underlying=underlying,
            option_type=option_side,
            strike_offset_points=strike_offset,
            expiry_offset_weeks=expiry_rule
        )
        
        if contract:
            return Instrument(
                symbol=contract["tradingsymbol"],
                underlying=underlying,
                exchange=contract["exchange"],
                segment=domain.segment,
                instrument_key=f"{contract['exchange']}:{contract['tradingsymbol']}",
                lot_size=int(contract["lot_size"]),
                tick_size=0.05,
                expiry=contract.get("expiry"),
                strike=float(contract["strike"]),
                option_type=option_side,
                source=InstrumentSource.UPSTOX_MASTER
            )
        return None
    
    async def _create_simulated_mcx_future(
        self,
        underlying: str,
        expiry_rule: int
    ) -> Optional[Instrument]:
        """Create simulated MCX future for paper mode."""
        try:
            domain = resolve_domain_by_underlying(underlying)
            lot_size = domain.get_lot_size(underlying)
            
            # Create synthetic instrument
            return Instrument(
                symbol=f"{underlying}_SIM",
                underlying=underlying,
                exchange="MCX",
                segment="MCX_FO",
                instrument_key=f"MCX_SIM_{underlying}",
                lot_size=lot_size,
                tick_size=0.05,
                source=InstrumentSource.PAPER_SIMULATED
            )
        except Exception as e:
            logger.warning(f"Failed to create simulated MCX future: {e}")
            return None
    
    async def _create_simulated_mcx_option(
        self,
        underlying: str,
        option_side: str,
        spot_price: float,
        strike_rule: str
    ) -> Optional[Instrument]:
        """Create simulated MCX option for paper mode."""
        try:
            domain = resolve_domain_by_underlying(underlying)
            lot_size = domain.get_lot_size(underlying)
            strike_interval = domain.get_strike_interval(underlying)
            
            # Calculate strike
            strike_offset = 0
            if strike_rule == "OTM1":
                strike_offset = strike_interval
            elif strike_rule == "ITM1":
                strike_offset = -strike_interval
            
            strike = round(spot_price + strike_offset)
            
            # Create synthetic option instrument
            return Instrument(
                symbol=f"{underlying}{option_side}{int(strike)}_SIM",
                underlying=underlying,
                exchange="MCX",
                segment="MCX_FO",
                instrument_key=f"MCX_SIM_{underlying}_{option_side}_{strike}",
                lot_size=lot_size,
                tick_size=0.05,
                strike=float(strike),
                option_type=option_side,
                source=InstrumentSource.PAPER_SIMULATED
            )
        except Exception as e:
            logger.warning(f"Failed to create simulated MCX option: {e}")
            return None
    
    async def _get_simulated_spot(self, underlying: str) -> Optional[float]:
        """Get simulated spot price for MCX commodity."""
        try:
            cache = await self.db.paper_quote_cache.find_one({"symbol": underlying})
            if cache:
                return float(cache.get("ltp", 0))
        except Exception as e:
            logger.warning(f"Failed to get simulated spot for {underlying}: {e}")
        return None

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
