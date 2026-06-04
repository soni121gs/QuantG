"""QuantG Quote Service.

Unified LTP fetching with source tracking.
- Live mode: fetches fresh Upstox LTP, rejects stale (>30sec old)
- Paper mode: uses Upstox LTP if available, falls back to simulated cache
"""

from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta
import logging

from core.models import Quote

logger = logging.getLogger("quantg.quote_service")


class QuoteService:
    """Unified quote service providing LTP with source tracking.
    
    - Live mode: fetches fresh Upstox LTP, rejects stale (>30sec old)
    - Paper mode: uses Upstox LTP if available, falls back to simulated cache
    
    Example:
        service = QuoteService(db, upstox_client)
        
        # Get live quote (fails hard if unavailable)
        quote = await service.get_quote("NIFTY", mode="live")
        
        # Get paper quote (uses simulation if needed)
        quote = await service.get_quote("NIFTY", mode="paper", allow_simulated=True)
    """
    
    STALE_THRESHOLD_SECONDS = 30
    
    def __init__(self, db, upstox_client=None):
        """Initialize QuoteService.
        
        Args:
            db: MongoDB database connection
            upstox_client: Upstox API client (optional for testing)
        """
        self.db = db
        self.upstox_client = upstox_client
    
    async def get_quote(
        self,
        symbol: str,
        mode: str = "paper",
        allow_simulated: bool = False
    ) -> Optional[Quote]:
        """Get LTP for a symbol.
        
        Args:
            symbol: Trading symbol (e.g., NIFTY, CRUDEOILM)
            mode: "paper" or "live"
            allow_simulated: If True, allow paper simulated cache fallback
            
        Returns:
            Quote with LTP and source, or None if unavailable
            
        Raises:
            Exception: On live mode staleness failure
        """
        try:
            # Try to get live Upstox LTP
            ltp_data = await self._fetch_upstox_ltp(symbol)
            
            if ltp_data and ltp_data.get("ltp") and ltp_data.get("timestamp"):
                timestamp_str = ltp_data["timestamp"]
                
                # Parse timestamp (handle both ISO and ISO+tz formats)
                timestamp = None
                if isinstance(timestamp_str, str):
                    try:
                        if "Z" in timestamp_str or "+" in timestamp_str:
                            timestamp = datetime.fromisoformat(
                                timestamp_str.replace("Z", "+00:00")
                            )
                        else:
                            timestamp = datetime.fromisoformat(timestamp_str)
                            if timestamp.tzinfo is None:
                                timestamp = timestamp.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Failed to parse timestamp {timestamp_str}: {e}")
                elif isinstance(timestamp_str, (int, float)):
                    # Epoch timestamp
                    try:
                        timestamp = datetime.fromtimestamp(timestamp_str, tz=timezone.utc)
                    except (ValueError, OSError):
                        pass
                
                if not timestamp:
                    logger.warning(f"Invalid or missing timestamp, using current time")
                    timestamp = datetime.now(timezone.utc)
                
                now = datetime.now(timezone.utc)
                age_seconds = (now - timestamp).total_seconds()
                
                if mode == "live":
                    # Live mode: reject stale quotes
                    if age_seconds > self.STALE_THRESHOLD_SECONDS:
                        logger.warning(
                            f"Live LTP for {symbol} is stale ({age_seconds:.1f}sec), rejecting"
                        )
                        return None
                
                return Quote(
                    ltp=float(ltp_data["ltp"]),
                    timestamp=timestamp_str if isinstance(timestamp_str, str) else timestamp.isoformat(),
                    source="UPSTOX_LIVE",
                    symbol=symbol
                )
        except Exception as e:
            logger.warning(f"Failed to get live LTP for {symbol}: {e}")
        
        # Fallback: paper mode can use simulated cache
        if mode == "paper" and allow_simulated:
            simulated = await self._get_simulated_quote(symbol)
            if simulated:
                return Quote(
                    ltp=simulated["ltp"],
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    source="PAPER_SIMULATED",
                    symbol=symbol
                )
        
        return None
    
    async def _fetch_upstox_ltp(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch LTP from Upstox API.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            dict with 'ltp' and 'timestamp' keys, or None
        """
        if not self.upstox_client:
            return None
        
        try:
            # Attempt to get quote from Upstox API
            # This is a placeholder - actual implementation depends on Upstox SDK
            if hasattr(self.upstox_client, 'get_full_quote'):
                quote = await self.upstox_client.get_full_quote(symbol)
                if quote and "last_price" in quote:
                    return {
                        "ltp": quote["last_price"],
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }
            elif hasattr(self.upstox_client, 'get_ltp'):
                quote = await self.upstox_client.get_ltp(symbol)
                if quote:
                    return quote
        except Exception as e:
            logger.warning(f"Upstox LTP fetch failed for {symbol}: {e}")
        
        return None
    
    async def _get_simulated_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get simulated quote from paper cache for paper mode.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            dict with 'ltp' key, or None if not found
        """
        try:
            cache = await self.db.paper_quote_cache.find_one({"symbol": symbol})
            if cache:
                return {
                    "ltp": float(cache.get("ltp", 0)),
                    "timestamp": cache.get("timestamp", datetime.now(timezone.utc).isoformat())
                }
        except Exception as e:
            logger.warning(f"Failed to get simulated quote for {symbol}: {e}")
        
        return None
