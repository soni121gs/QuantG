"""QuantG Core Event Store.

A unified append-only ledger collection storing all vital trading system events.
Allows auditability and enables AI-based diagnostics explaining rejections, trades, and strategy scorecards.
"""
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import uuid
import logging

logger = logging.getLogger("quantg.core.event_store")

class CoreEventStore:
    def __init__(self, db):
        self.db = db

    async def log_event(
        self,
        event_type: str,               # e.g., "STRATEGY_SIGNAL", "RISK_BLOCKED", "PAPER_FILLED"
        strategy_id: str,
        user_id: str,
        metadata: Dict[str, Any],
        run_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Saves a structured operational event into the core_events collection."""
        now_str = datetime.now(timezone.utc).isoformat()
        
        event_doc = {
            "_id": str(uuid.uuid4()),
            "event_type": str(event_type).upper(),
            "strategy_id": strategy_id,
            "user_id": user_id,
            "run_id": run_id or f"run_{uuid.uuid4().hex[:12]}",
            "metadata": metadata,
            "created_at": now_str
        }
        
        try:
            await self.db.core_events.insert_one(event_doc)
            logger.info(f"CORE EVENT STORE: Logged {event_type} for strategy {strategy_id}")
        except Exception as e:
            logger.warning(f"Failed writing event to core_events store: {e}")
            
        return event_doc
