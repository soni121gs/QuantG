"""Compatibility tick manager for the Upstox-only runtime.

Upstox market data is managed by ``brokers.upstox_gateway``.  This class keeps
the old server interface available without importing or starting Kite.
"""
from __future__ import annotations

from typing import Any, Dict, List


class RealtimeTickManager:
    def start_for_user(self, user_id: str, *_args: Any, **_kwargs: Any) -> None:
        return None

    def stop_for_user(self, user_id: str) -> None:
        return None

    def stop(self) -> None:
        return None

    def has_live_ticks(self, user_id: str) -> bool:
        return False

    def is_running(self, user_id: str) -> bool:
        return False

    def has_symbol(self, user_id: str, symbol: str) -> bool:
        return False

    def get_candles(self, user_id: str, symbol: str, bars: int = 250) -> List[Dict[str, Any]]:
        return []

    def status_info(self, user_id: str) -> Dict[str, Any]:
        return {
            "connected": False,
            "subscribed_tokens": 0,
            "last_connect_at": None,
            "last_disconnect_at": None,
            "last_error": "Legacy Kite ticker removed; Upstox feed is the only realtime source.",
            "last_tick_at": None,
            "auth_failed": False,
            "reconnect_attempts": 0,
            "websocket_url": "upstox_gateway",
            "removed_legacy_ticker": True,
        }
