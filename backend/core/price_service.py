"""QuantG Price Service.

One source of truth for instrument and spot pricing.
Enforces that Live mode never uses mock prices and checks price feed age for freshness.
"""
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import logging

logger = logging.getLogger("quantg.price_service")

class PriceService:
    def __init__(self, db, upstox_client=None, tick_manager=None):
        self.db = db
        self.upstox = upstox_client
        self.tick_manager = tick_manager

    async def get_price(
        self,
        instrument_key: str,
        mode: str,                         # "live" | "paper" | "backtest"
        allow_simulation: bool = False,
        historical_candle: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Gets current LTP and details from active sources based on operational mode."""
        now_ms = int(time.time() * 1000)
        
        # 1. Backtest mode uses strict candle history (prevents lookahead bias)
        if mode == "backtest":
            if not historical_candle:
                raise ValueError("Historical candle is required when requesting price in backtest mode.")
            return {
                "price": float(historical_candle["close"]),
                "source": "backtest_candle",
                "feed_age_ms": 0,
                "is_fresh": True,
                "instrument_key": instrument_key,
                "timestamp": historical_candle.get("date")
            }

        # 2. Live mode: Must use high-quality live data
        if mode == "live":
            # Attempt websocket ticks first
            if self.tick_manager:
                tick = await self.tick_manager.get_last_tick(instrument_key)
                if tick:
                    tick_time = tick.get("timestamp", now_ms)
                    age = max(0, now_ms - tick_time)
                    if age < 5000:  # Freshness threshold of 5s
                        return {
                            "price": float(tick["last_price"]),
                            "source": "websocket_v3",
                            "feed_age_ms": age,
                            "is_fresh": True,
                            "instrument_key": instrument_key
                        }
            
            # REST Fallback
            if self.upstox:
                try:
                    quote = await self.upstox.get_full_quote(instrument_key)
                    if quote and "last_price" in quote:
                        return {
                            "price": float(quote["last_price"]),
                            "source": "upstox_rest_quote",
                            "feed_age_ms": 0,
                            "is_fresh": True,
                            "instrument_key": instrument_key
                        }
                except Exception as exc:
                    logger.warning(f"Upstox REST quote fetch failed for {instrument_key}: {exc}")

            raise RuntimeError(f"Live price fetch failed: No active feed or REST quote for instrument {instrument_key}")

        # 3. Paper mode: Uses real quote or websocket, falls back to simulated if allowed
        if mode == "paper":
            if self.tick_manager:
                tick = await self.tick_manager.get_last_tick(instrument_key)
                if tick:
                    tick_time = tick.get("timestamp", now_ms)
                    age = max(0, now_ms - tick_time)
                    if age < 15000:  # Allow up to 15s in paper mode
                        return {
                            "price": float(tick["last_price"]),
                            "source": "websocket_v3",
                            "feed_age_ms": age,
                            "is_fresh": True,
                            "instrument_key": instrument_key
                        }

            if self.upstox:
                try:
                    quote = await self.upstox.get_full_quote(instrument_key)
                    if quote and "last_price" in quote:
                        return {
                            "price": float(quote["last_price"]),
                            "source": "upstox_rest_quote",
                            "feed_age_ms": 0,
                            "is_fresh": True,
                            "instrument_key": instrument_key
                        }
                except Exception:
                    pass

            if allow_simulation:
                # Provide a calculated simulated price
                import math
                bucket = int(time.time() // 60)
                seed = hash(instrument_key) % 1000
                base = 100.0  # Safe default base price
                drift = math.sin(bucket / 11.0 + seed * 1.7) * (base * 0.004)
                noise = math.sin(bucket / 5.0 + seed * 8.3) * (base * 0.001)
                sim_price = round(base + drift + noise, 2)
                
                return {
                    "price": float(sim_price),
                    "source": "paper_simulator",
                    "feed_age_ms": 0,
                    "is_fresh": False,
                    "instrument_key": instrument_key,
                    "warning": "Simulated price in use."
                }
                
            raise RuntimeError(f"Paper price fetch failed: No active feeds and simulation disallowed for {instrument_key}")

        raise ValueError(f"Unsupported price service mode: {mode}")
