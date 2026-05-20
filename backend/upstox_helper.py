"""Upstox adapter scaffold for QuantG.

The app can store credentials and show readiness now. Live routing is kept
disabled until the account-specific OAuth and websocket subscriptions are
verified in production.
"""
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
            "reason": "no_keys",
        }
    if not api_key:
        return {
            "connected": False,
            "keys_saved": True,
            "sdk_available": sdk_available(),
            "reason": "credential_decrypt_failed",
        }
    if not sdk_available():
        return {
            "connected": False,
            "keys_saved": True,
            "sdk_available": False,
            "reason": "upstox_client_not_installed",
            "client_id": keys.get("user_id_at_broker"),
        }
    return {
        "connected": False,
        "keys_saved": True,
        "sdk_available": True,
        "reason": "oauth_session_not_configured",
        "client_id": keys.get("user_id_at_broker"),
    }
