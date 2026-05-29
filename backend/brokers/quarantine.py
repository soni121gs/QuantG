"""Quarantined legacy broker integration module for QuantG.

Holds Kite Connect and Kotak Neo wrappers to separate them from the primary Upstox pathways.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import kite_helper
from core import db

logger = logging.getLogger("quantg.quarantine")

async def get_user_kite(user_id: str):
    """Return (kite_instance, status_dict). kite is None if not connected/expired."""
    from server import decrypt_secret
    keys = await db.broker_keys.find_one({"user_id": user_id, "broker": "zerodha"})
    access_token = decrypt_secret(keys.get("access_token")) if keys else None
    api_key = decrypt_secret(keys.get("api_key")) if keys else None
    if not keys or not access_token:
        return None, {"connected": False, "reason": "no_token"}
    if not kite_helper.is_token_valid(keys.get("access_token_expires_at")):
        return None, {"connected": False, "reason": "expired", "kite_user_id": keys.get("kite_user_id")}
    if not api_key:
        return None, {"connected": False, "reason": "credential_decrypt_failed"}
    kite = kite_helper.make_kite(api_key, access_token)
    return kite, {"connected": True, "kite_user_id": keys.get("kite_user_id"),
                  "expires_at": keys["access_token_expires_at"]}

async def get_user_kotak_status(user_id: str) -> Dict[str, Any]:
    return {"connected": False, "reason": "kotak_neo_removed"}

async def get_user_kotak_gateway(user_id: str, fresh: bool = False) -> Optional[Any]:
    return None
