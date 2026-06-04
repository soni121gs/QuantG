"""QuantG Quote Service.

Unified LTP fetching with source tracking.
- Live mode: fetches fresh Upstox LTP, rejects stale (>30sec old)
- Paper mode: uses Upstox LTP if available, falls back to simulated cache
"""

import asyncio
import inspect
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
        self.last_diagnostics: Dict[str, Any] = {}

    def _diag(self, **values: Any) -> None:
        self.last_diagnostics.update({k: v for k, v in values.items() if v is not None})

    @staticmethod
    def _normalize_key(symbol: str) -> str:
        return str(symbol or "").strip()

    @staticmethod
    def _real_method(obj: Any, name: str) -> Any:
        if obj is None:
            return None
        method = getattr(obj, name, None)
        if not callable(method):
            return None
        if name in getattr(obj, "__dict__", {}):
            return method
        if hasattr(type(obj), name):
            return method
        return None

    @staticmethod
    def _parse_timestamp(timestamp_value: Any) -> Optional[datetime]:
        if timestamp_value in (None, ""):
            return None
        if isinstance(timestamp_value, datetime):
            return timestamp_value if timestamp_value.tzinfo else timestamp_value.replace(tzinfo=timezone.utc)
        if isinstance(timestamp_value, (int, float)):
            try:
                value = float(timestamp_value)
                if value > 10_000_000_000:
                    value = value / 1000
                return datetime.fromtimestamp(value, tz=timezone.utc)
            except (ValueError, OSError):
                return None
        try:
            parsed = datetime.fromisoformat(str(timestamp_value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    
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
            lookup_key = self._normalize_key(symbol)
            self.last_diagnostics = {
                "subscribed_key": lookup_key,
                "cache_lookup_key": lookup_key,
                "cache_hit": False,
                "quote_timestamp": None,
                "quote_age_sec": None,
                "quote_reject_reason": None,
            }
            # Try to get live Upstox LTP
            ltp_data = await self._fetch_upstox_ltp(lookup_key)
            
            if ltp_data and ltp_data.get("ltp") and ltp_data.get("timestamp"):
                timestamp_str = ltp_data["timestamp"]
                
                # Parse timestamp (handle both ISO and ISO+tz formats)
                timestamp = self._parse_timestamp(timestamp_str)
                
                if not timestamp:
                    logger.warning(f"Invalid or missing timestamp, using current time")
                    timestamp = datetime.now(timezone.utc)
                
                now = datetime.now(timezone.utc)
                age_seconds = (now - timestamp).total_seconds()
                
                if mode == "live":
                    # Live mode: reject stale quotes
                    if age_seconds > self.STALE_THRESHOLD_SECONDS:
                        self._diag(
                            cache_hit=bool(ltp_data.get("cache_hit")),
                            quote_timestamp=timestamp.isoformat(),
                            quote_age_sec=round(age_seconds, 3),
                            quote_reject_reason="stale_upstox_quote",
                        )
                        logger.warning(
                            f"Live LTP for {symbol} is stale ({age_seconds:.1f}sec), rejecting"
                        )
                        return None
                if age_seconds > self.STALE_THRESHOLD_SECONDS:
                    self._diag(
                        cache_hit=bool(ltp_data.get("cache_hit")),
                        quote_timestamp=timestamp.isoformat(),
                        quote_age_sec=round(age_seconds, 3),
                        quote_reject_reason="stale_upstox_quote",
                    )
                    return None
                
                self._diag(
                    cache_hit=bool(ltp_data.get("cache_hit")),
                    quote_timestamp=timestamp.isoformat(),
                    quote_age_sec=round(age_seconds, 3),
                    quote_reject_reason=None,
                    quote_source=ltp_data.get("source") or "UPSTOX_LIVE",
                )
                return Quote(
                    ltp=float(ltp_data["ltp"]),
                    timestamp=timestamp_str if isinstance(timestamp_str, str) else timestamp.isoformat(),
                    source=ltp_data.get("source") or "UPSTOX_LIVE",
                    symbol=lookup_key
                )
        except Exception as e:
            logger.warning(f"Failed to get live LTP for {symbol}: {e}")
            self._diag(quote_reject_reason=f"quote_fetch_exception:{e}")
        
        # Fallback: paper mode can use simulated cache
        if mode == "paper" and allow_simulated:
            simulated = await self._get_simulated_quote(self._normalize_key(symbol))
            if simulated:
                self._diag(
                    cache_hit=True,
                    quote_timestamp=simulated.get("timestamp"),
                    quote_reject_reason=None,
                    quote_source=simulated.get("source") or "PAPER_SIMULATED",
                )
                return Quote(
                    ltp=simulated["ltp"],
                    timestamp=simulated.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                    source=simulated.get("source") or "PAPER_SIMULATED",
                    symbol=self._normalize_key(symbol)
                )
        self._diag(quote_reject_reason=self.last_diagnostics.get("quote_reject_reason") or "upstox_fresh_quote_unavailable")
        return None
    
    async def _fetch_upstox_ltp(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Fetch LTP from Upstox API.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            dict with 'ltp' and 'timestamp' keys, or None
        """
        if not self.upstox_client:
            self._diag(quote_reject_reason="upstox_gateway_unavailable")
            return None
        
        try:
            tick_method = self._real_method(self.upstox_client, "latest_tick")
            if callable(tick_method):
                tick = tick_method(symbol)
                if inspect.isawaitable(tick):
                    tick = await tick
                if isinstance(tick, dict) and tick.get("ltp"):
                    ts = tick.get("timestamp") or tick.get("received_at") or tick.get("last_trade_time")
                    result = {
                        "ltp": float(tick["ltp"]),
                        "timestamp": ts or datetime.now(timezone.utc).isoformat(),
                        "source": "UPSTOX_LIVE",
                        "cache_hit": True,
                    }
                    await self._store_quote_cache(symbol, result)
                    return result

            subscribe_method = self._real_method(self.upstox_client, "start_market_data_ws")
            if callable(subscribe_method) and not inspect.iscoroutinefunction(subscribe_method):
                try:
                    self._diag(subscribed_key=symbol)
                    sub_res = await asyncio.to_thread(subscribe_method, [symbol], "ltpc")
                    self._diag(subscription_result=sub_res)
                except Exception as exc:
                    self._diag(subscription_result={"ok": False, "error": str(exc)[:200]})

            # Attempt to get quote from Upstox API
            # This is a placeholder - actual implementation depends on Upstox SDK
            full_quote_method = self._real_method(self.upstox_client, "get_full_quote")
            if full_quote_method:
                quote = await full_quote_method(symbol) if inspect.iscoroutinefunction(full_quote_method) else full_quote_method(symbol)
                if quote and "last_price" in quote:
                    result = {
                        "ltp": quote["last_price"],
                        "timestamp": quote.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                        "source": "UPSTOX_LIVE",
                        "cache_hit": False,
                    }
                    await self._store_quote_cache(symbol, result)
                    return result
            get_ltp_method = self._real_method(self.upstox_client, "get_ltp")
            if get_ltp_method:
                quote = await get_ltp_method(symbol) if inspect.iscoroutinefunction(get_ltp_method) else get_ltp_method(symbol)
                if quote:
                    return quote
            market_quote_method = self._real_method(self.upstox_client, "get_market_quote")
            if market_quote_method:
                quote = await asyncio.to_thread(market_quote_method, [symbol])
                parser = getattr(self.upstox_client, "parse_quote_ltp", None)
                ltp = parser(quote, symbol) if parser else None
                if not ltp:
                    node = ((quote or {}).get("data") or {}).get(symbol) or {}
                    ltp = node.get("last_price") or node.get("ltp")
                if ltp:
                    result = {
                        "ltp": float(ltp),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "source": "UPSTOX_LIVE",
                        "cache_hit": False,
                    }
                    await self._store_quote_cache(symbol, result)
                    return result
        except Exception as e:
            logger.warning(f"Upstox LTP fetch failed for {symbol}: {e}")
            self._diag(quote_reject_reason=f"upstox_ltp_fetch_failed:{e}")
        
        return None
    
    async def _get_simulated_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get simulated quote from paper cache for paper mode.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            dict with 'ltp' key, or None if not found
        """
        try:
            self._diag(cache_lookup_key=symbol)
            cache = await self.db.paper_quote_cache.find_one({
                "$or": [
                    {"symbol": symbol},
                    {"instrument_key": symbol},
                ]
            })
            if cache:
                self._diag(cache_hit=True)
                return {
                    "ltp": float(cache.get("ltp", 0)),
                    "timestamp": cache.get("timestamp", datetime.now(timezone.utc).isoformat()),
                    "source": cache.get("source") or "PAPER_SIMULATED",
                }
        except Exception as e:
            logger.warning(f"Failed to get simulated quote for {symbol}: {e}")
            self._diag(quote_reject_reason=f"cache_lookup_failed:{e}")
        
        return None

    async def _store_quote_cache(self, symbol: str, quote: Dict[str, Any]) -> None:
        try:
            await self.db.paper_quote_cache.update_one(
                {"symbol": symbol},
                {"$set": {
                    "symbol": symbol,
                    "instrument_key": symbol,
                    "ltp": float(quote["ltp"]),
                    "timestamp": quote.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                    "source": quote.get("source") or "UPSTOX_LIVE",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }},
                upsert=True,
            )
        except Exception as exc:
            logger.debug("Quote cache write skipped for %s: %s", symbol, exc)
