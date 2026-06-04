"""QuantG Market Session Service.

Segment-aware market session checks for NSE, BSE, and MCX.
Each domain has its own session window, allowing MCX to trade when NSE is closed.
"""

import os
from enum import Enum
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
import logging

from core.market_domains import DomainType

logger = logging.getLogger("quantg.market_session_service")

IST_OFFSET = timedelta(hours=5, minutes=30)
NSE_OPEN_MINUTE = 9 * 60 + 15  # 09:15 IST
NSE_CLOSE_MINUTE = 15 * 60 + 30  # 15:30 IST
MCX_OPEN_MINUTE = int(os.environ.get("MCX_OPEN_MINUTE", str(9 * 60)))  # 09:00 IST
MCX_CLOSE_MINUTE = int(os.environ.get("MCX_CLOSE_MINUTE", str(23 * 60 + 30)))  # 23:30 IST

SEGMENT_WINDOWS = {
    DomainType.NSE_FO: (NSE_OPEN_MINUTE, NSE_CLOSE_MINUTE, "NSE F&O"),
    DomainType.BSE_FO: (NSE_OPEN_MINUTE, NSE_CLOSE_MINUTE, "BSE F&O"),
    DomainType.MCX_FO: (MCX_OPEN_MINUTE, MCX_CLOSE_MINUTE, "MCX commodity"),
}


class MarketSessionService:
    """Segment-aware market session checks.
    
    Each domain (NSE_FO, BSE_FO, MCX_FO) has its own session window.
    This allows MCX to run even when NSE is closed.
    
    Example:
        # Check if MCX can trade now
        is_mcx_open = MarketSessionService.is_segment_open(DomainType.MCX_FO)
        
        # Get status for NSE
        nse_status = MarketSessionService.get_segment_status(DomainType.NSE_FO)
    """
    
    @staticmethod
    def get_ist_now(now_utc: Optional[datetime] = None) -> datetime:
        """Convert UTC datetime to IST (UTC+5:30).
        
        Args:
            now_utc: UTC datetime. If None, uses current time.
            
        Returns:
            datetime: IST datetime
        """
        utc = now_utc or datetime.now(timezone.utc)
        return utc + IST_OFFSET
    
    @staticmethod
    def is_segment_open(domain: DomainType, now_utc: Optional[datetime] = None) -> bool:
        """Check if a specific market segment is open.
        
        Args:
            domain: DomainType enum (NSE_FO, MCX_FO, BSE_FO), MarketDomain object, or string representation.
            now_utc: Optional datetime for testing
            
        Returns:
            bool: True if segment is open for trading
        """
        # Resolve domain to DomainType enum
        from core.market_domains import DomainType
        resolved_domain = None
        if isinstance(domain, DomainType):
            resolved_domain = domain
        elif hasattr(domain, 'name') and isinstance(domain.name, DomainType):
            resolved_domain = domain.name
        elif isinstance(domain, str):
            try:
                resolved_domain = DomainType(domain.upper())
            except ValueError:
                try:
                    resolved_domain = DomainType[domain.upper()]
                except KeyError:
                    pass
        elif hasattr(domain, 'name') and isinstance(domain.name, str):
            try:
                resolved_domain = DomainType(domain.name.upper())
            except ValueError:
                try:
                    resolved_domain = DomainType[domain.name.upper()]
                except KeyError:
                    pass

        if resolved_domain is None:
            logger.warning(f"Unknown domain parameter type or value {domain}, returning False")
            return False

        window = SEGMENT_WINDOWS.get(resolved_domain)
        if not window:
            logger.warning(f"Unknown domain {resolved_domain}, returning False")
            return False
        
        open_minute, close_minute, _ = window
        ist_now = MarketSessionService.get_ist_now(now_utc)
        minutes = ist_now.hour * 60 + ist_now.minute
        day_of_week = ist_now.weekday()
        
        # Check weekday (0-4 = Mon-Fri, 5-6 = Sat-Sun)
        if day_of_week >= 5:
            logger.debug(f"Segment {resolved_domain} closed: weekend")
            return False
        
        # Check if in trading window
        is_open = open_minute <= minutes <= close_minute
        if not is_open:
            logger.debug(
                f"Segment {resolved_domain} closed: {minutes:04d} minutes not in "
                f"[{open_minute:04d}, {close_minute:04d}]"
            )
        return is_open
    
    @staticmethod
    def get_segment_status(domain: DomainType, now_utc: Optional[datetime] = None) -> Dict[str, Any]:
        """Get detailed status for a market segment.
        
        Args:
            domain: DomainType enum, MarketDomain object, or string representation.
            now_utc: Optional datetime for testing
            
        Returns:
            dict: Status information including open/closed, hours, current time
        """
        # Resolve domain to DomainType enum
        from core.market_domains import DomainType
        resolved_domain = None
        if isinstance(domain, DomainType):
            resolved_domain = domain
        elif hasattr(domain, 'name') and isinstance(domain.name, DomainType):
            resolved_domain = domain.name
        elif isinstance(domain, str):
            try:
                resolved_domain = DomainType(domain.upper())
            except ValueError:
                try:
                    resolved_domain = DomainType[domain.upper()]
                except KeyError:
                    pass
        elif hasattr(domain, 'name') and isinstance(domain.name, str):
            try:
                resolved_domain = DomainType(domain.name.upper())
            except ValueError:
                try:
                    resolved_domain = DomainType[domain.name.upper()]
                except KeyError:
                    pass

        if resolved_domain is None:
            domain_str = domain.value if hasattr(domain, 'value') else str(domain)
            return {
                "segment": domain_str,
                "open": False,
                "reason": "Unsupported segment"
            }

        window = SEGMENT_WINDOWS.get(resolved_domain)
        if not window:
            domain_str = resolved_domain.value if hasattr(resolved_domain, 'value') else str(resolved_domain)
            return {
                "segment": domain_str,
                "open": False,
                "reason": "Unsupported segment"
            }
        
        open_minute, close_minute, label = window
        ist_now = MarketSessionService.get_ist_now(now_utc)
        minutes = ist_now.hour * 60 + ist_now.minute
        day_of_week = ist_now.weekday()
        is_open = MarketSessionService.is_segment_open(resolved_domain, now_utc)
        
        # Format trading hours
        open_hour = open_minute // 60
        open_min = open_minute % 60
        close_hour = close_minute // 60
        close_min = close_minute % 60
        
        return {
            "segment": resolved_domain.value if hasattr(resolved_domain, 'value') else str(resolved_domain),
            "label": label,
            "open": is_open,
            "current_time_ist": ist_now.isoformat(),
            "current_minutes": minutes,
            "trading_hours": f"{open_hour:02d}:{open_min:02d} - {close_hour:02d}:{close_min:02d}",
            "day_of_week": day_of_week,
            "is_weekend": day_of_week >= 5
        }
    
    @staticmethod
    def get_all_segment_statuses(now_utc: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
        """Get status for all market segments.
        
        Returns:
            dict: Status for NSE_FO, BSE_FO, and MCX_FO
        """
        return {
            "NSE_FO": MarketSessionService.get_segment_status(DomainType.NSE_FO, now_utc),
            "BSE_FO": MarketSessionService.get_segment_status(DomainType.BSE_FO, now_utc),
            "MCX_FO": MarketSessionService.get_segment_status(DomainType.MCX_FO, now_utc),
        }
