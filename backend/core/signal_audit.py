"""Best-effort, secret-free signal decision audit records."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


_FIELDS = ("id", "user_id", "strategy_id", "symbol", "underlying", "action", "mode", "confidence", "price", "regime", "regime_fine", "regime_fine_data_quality", "regime_fine_router_allowed", "regime_fine_bar_count", "regime_fine_first_bar_ist", "regime_fine_source", "quote_source", "quote_timestamp", "quote_age_sec", "option_contract", "candidate_contracts", "option_quality_score", "structure", "spread", "required_capital", "status", "rejection_reason", "rejection_detail", "visual_config", "trend_context", "created_at")


def build_signal_audit(signal: Dict[str, Any], *, stage: str, decision: str, reason_code: Optional[str] = None, detail: Any = None) -> Dict[str, Any]:
    record = {"signal_id": signal.get("id"), "stage": stage, "decision": decision, "recorded_at": datetime.now(timezone.utc).isoformat()}
    for field in _FIELDS:
        if field in signal:
            record[field] = _safe(signal[field])
    if reason_code:
        record["reason_code"] = reason_code
    if detail is not None:
        record["decision_detail"] = _safe(detail)
    return record


async def write_signal_audit(db: Any, signal: Dict[str, Any], *, stage: str, decision: str, reason_code: Optional[str] = None, detail: Any = None) -> None:
    try:
        await db.signal_audits.insert_one(build_signal_audit(signal, stage=stage, decision=decision, reason_code=reason_code, detail=detail))
    except Exception:
        return


def _safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items() if str(k).lower() not in {"token", "access_token", "authorization", "api_key", "secret"}}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
