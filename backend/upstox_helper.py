"""Upstox status helpers for QuantG."""
from __future__ import annotations

from typing import Any, Dict, Optional

try:  # pragma: no cover - optional production SDK
    import upstox_client  # type: ignore
except Exception:  # pragma: no cover
    upstox_client = None


def sdk_available() -> bool:
    return upstox_client is not None


def status_from_keys(keys: Optional[Dict[str, Any]], api_key: Optional[str]) -> Dict[str, Any]:
    if not keys:
        return {
            "connected": False,
            "keys_saved": False,
            "sdk_available": sdk_available(),
            "requests_gateway_available": True,
            "reason": "no_keys",
        }
    if not api_key:
        return {
            "connected": False,
            "keys_saved": True,
            "sdk_available": sdk_available(),
            "requests_gateway_available": True,
            "reason": "credential_decrypt_failed",
        }
    return {
        "connected": False,
        "keys_saved": True,
        "sdk_available": sdk_available(),
        "requests_gateway_available": True,
        "reason": "no_token",
        "client_id": keys.get("user_id_at_broker"),
        "redirect_uri": keys.get("redirect_uri"),
    }
