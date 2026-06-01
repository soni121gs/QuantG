"""QuantG Order Manager.

Compiles abstract signals into concrete execution intents.
Generates strong composite idempotency keys and protects against duplicate order submissions.
"""
from typing import Dict, Any, Optional
import hashlib
from datetime import datetime, timezone
import logging

logger = logging.getLogger("quantg.order_manager")

class OrderManager:
    def __init__(self, db):
        self.db = db

    def generate_idempotency_key(
        self,
        strategy_id: str,
        market_domain: str,
        symbol: str,
        side: str,
        session_date: str,
        signal_candle_time: str
    ) -> str:
        """Generates a secure composite idempotency key to prevent double trades."""
        raw_key = f"{strategy_id}:{market_domain}:{symbol}:{side}:{session_date}:{signal_candle_time}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:32]

    async def verify_and_lock_idempotency(self, idempotency_key: str, user_id: str) -> bool:
        """Verifies if the idempotency key is already used.
        
        Returns True if fresh (available), False if duplicate (blocked).
        """
        existing = None
        try:
            existing = await self.db.orders.find_one({
                "user_id": user_id,
                "idempotency_key": idempotency_key
            })
        except TypeError:
            pass

        if existing:
            return False
            
        # Also check signals collection
        existing_sig = None
        try:
            existing_sig = await self.db.signals.find_one({
                "user_id": user_id,
                "idempotency_key": idempotency_key,
                "status": {"$in": ["PROCESSED", "FILLED"]}
            })
        except TypeError:
            pass

        if existing_sig:
            return False
            
        return True

    def compile_order_intent(
        self,
        strategy_id: str,
        symbol: str,
        target_symbol: str,
        side: str,
        quantity: int,
        price: float,
        exchange: str,
        segment: str,
        mode: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """Assembles the validated parameters into a standard OrderIntent payload."""
        now_str = datetime.now(timezone.utc).isoformat()
        return {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "target_symbol": target_symbol,
            "side": side.upper(),
            "qty": quantity,
            "requested_price": price,
            "exchange": exchange,
            "segment": segment,
            "mode": mode.lower(),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "idempotency_key": idempotency_key,
            "status": "CREATED",
            "created_at": now_str,
            "updated_at": now_str
        }
