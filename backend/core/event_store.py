"""QuantG Core Event Store.

A unified append-only ledger collection storing all vital trading system events.
Allows auditability and enables AI-based diagnostics explaining rejections, trades, and strategy scorecards.
"""
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
import uuid
import logging
from pydantic import BaseModel

logger = logging.getLogger("quantg.core.event_store")


class SignalLifecycleEventPayload(BaseModel):
    signal_id: Optional[str] = None
    signal_status: Optional[str] = None
    reason_code: Optional[str] = None
    human_reason: Optional[str] = None
    order_id: Optional[str] = None
    skipped_signal_id: Optional[str] = None
    symbol: Optional[str] = None
    target_symbol: Optional[str] = None
    action: Optional[str] = None
    mode: Optional[str] = None
    priority_score: Optional[float] = None
    selected_signal_id: Optional[str] = None
    competing_signal_ids: Optional[List[str]] = None
    detail: Optional[Dict[str, Any]] = None


class CoreEventEnvelope(BaseModel):
    event_id: str
    event_type: str
    schema_version: int = 1
    occurred_at: str
    user_id: str
    strategy_id: Optional[str] = None
    correlation_id: str
    causation_id: str
    idempotency_key: str
    source_module: str
    payload: Dict[str, Any]


def _model_dump(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


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

    async def log_signal_event(
        self,
        event_type: str,
        sig: Dict[str, Any],
        payload: Optional[Dict[str, Any]] = None,
        causation_id: Optional[str] = None,
        source_module: str = "signal_manager",
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Publish a Stage 1 signal lifecycle event without owning signal state."""
        now_str = datetime.now(timezone.utc).isoformat()
        signal_id = str(sig.get("id") or sig.get("_id") or "")
        event_name = str(event_type).upper()
        event_id = f"evt_{uuid.uuid4().hex}"
        payload_data = {
            "signal_id": signal_id or None,
            "signal_status": sig.get("status"),
            "symbol": str(sig.get("symbol") or "").upper() or None,
            "target_symbol": sig.get("target_symbol"),
            "action": str(sig.get("action") or "").upper() or None,
            "mode": sig.get("mode"),
        }
        payload_data.update(payload or {})
        payload_model = SignalLifecycleEventPayload(**payload_data)
        envelope = CoreEventEnvelope(
            event_id=event_id,
            event_type=event_name,
            occurred_at=now_str,
            user_id=str(sig.get("user_id") or ""),
            strategy_id=sig.get("strategy_id"),
            correlation_id=f"corr:{signal_id}",
            causation_id=causation_id or f"record:signals:{signal_id}",
            idempotency_key=idempotency_key or f"signal:{event_name}:{signal_id}",
            source_module=source_module,
            payload=_model_dump(payload_model),
        )
        event_doc = {
            "_id": event_id,
            "created_at": now_str,
            **_model_dump(envelope),
        }

        try:
            await self.db.core_events.insert_one(event_doc)
            logger.info("CORE EVENT STORE: Logged %s for signal %s", event_name, signal_id)
        except Exception as e:
            logger.warning("Failed writing signal event to core_events store: %s", e)

        return event_doc
