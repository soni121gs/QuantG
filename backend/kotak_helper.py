"""Kotak Neo adapter scaffold.

This module is intentionally conservative: it detects whether the official
Neo SDK is installed and validates that credentials are present, but it does
not place orders until the account-specific login/session flow is verified.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("quantg.kotak")

try:  # pragma: no cover - depends on optional production install
    from neo_api_client import NeoAPI  # type: ignore
except Exception:  # pragma: no cover
    NeoAPI = None


def sdk_available() -> bool:
    return NeoAPI is not None


def status_from_keys(keys: Optional[Dict[str, Any]], consumer_key: Optional[str]) -> Dict[str, Any]:
    if not keys:
        return {
            "connected": False,
            "keys_saved": False,
            "sdk_available": sdk_available(),
            "reason": "no_keys",
        }
    if not consumer_key:
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
            "reason": "neo_api_client_not_installed",
        }
    return {
        "connected": False,
        "keys_saved": True,
        "sdk_available": True,
        "reason": "ready_to_connect",
        "client_id": keys.get("user_id_at_broker"),
    }
