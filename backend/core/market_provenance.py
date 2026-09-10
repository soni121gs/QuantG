"""Small, dependency-free provenance and quote reconciliation helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def parse_timestamp(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            number = float(value) / (1000 if float(value) > 10_000_000_000 else 1)
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    try:
        value = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def quote_age_seconds(timestamp: Any, *, now: Optional[datetime] = None) -> Optional[float]:
    parsed = parse_timestamp(timestamp)
    if not parsed:
        return None
    current = now or datetime.now(timezone.utc)
    return round(max(0.0, (current - parsed).total_seconds()), 3)


def reconcile_quotes(rest: Optional[Dict[str, Any]], websocket: Optional[Dict[str, Any]], *, tolerance_bps: float = 5.0) -> Dict[str, Any]:
    rest_ltp = _number((rest or {}).get("ltp") or (rest or {}).get("last_price"))
    ws_ltp = _number((websocket or {}).get("ltp") or (websocket or {}).get("last_price"))
    result: Dict[str, Any] = {"status": "INVALID", "rest_ltp": rest_ltp, "websocket_ltp": ws_ltp}
    if rest_ltp is None:
        result["status"] = "MISSING_REST" if ws_ltp is not None else "INVALID"
        return result
    if ws_ltp is None:
        result["status"] = "MISSING_WEBSOCKET"
        return result
    diff = abs(rest_ltp - ws_ltp)
    diff_bps = diff / max(abs(ws_ltp), 0.000001) * 10000
    result.update({"absolute_diff": round(diff, 8), "difference_bps": round(diff_bps, 3), "status": "MATCH" if diff_bps <= tolerance_bps else "MISMATCH"})
    return result


def _number(value: Any) -> Optional[float]:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None
