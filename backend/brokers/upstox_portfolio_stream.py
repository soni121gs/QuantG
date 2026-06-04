"""Upstox Portfolio Stream client for order/position truth updates."""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import quote

try:
    import websocket
except Exception:  # pragma: no cover
    websocket = None


logger = logging.getLogger("quantg.upstox_portfolio_stream")


class UpstoxPortfolioStream:
    def __init__(
        self,
        *,
        access_token_getter: Callable[[], Optional[str]],
        event_callback: Callable[[Dict[str, Any]], None],
        update_types: Iterable[str] = ("order", "gtt_order", "position", "holding"),
    ) -> None:
        self._access_token_getter = access_token_getter
        self._event_callback = event_callback
        self._update_types = tuple(update_types)
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._ws_app: Optional[Any] = None
        self._running = False
        self._connected = False
        self._last_error: Optional[str] = None
        self._events = 0

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "connected": self._connected,
                "last_error": self._last_error,
                "events": self._events,
                "update_types": list(self._update_types),
            }

    def start(self) -> Dict[str, Any]:
        if websocket is None:
            return {"ok": False, "reason": "websocket-client missing"}
        with self._lock:
            if self._running:
                return {"ok": True, "started": False, "reason": "already_running", "status": self.status()}
            token = self._access_token_getter()
            if not token:
                return {"ok": False, "reason": "no_token"}
            self._running = True
            self._thread = threading.Thread(target=self._run, name="upstox-portfolio-stream", daemon=True)
            self._thread.start()
        return {"ok": True, "started": True}

    def stop(self) -> None:
        with self._lock:
            self._running = False
            app = self._ws_app
        if app:
            try:
                app.close()
            except Exception:
                pass

    def _url(self) -> str:
        update_types = quote(",".join(self._update_types))
        return f"wss://api.upstox.com/v2/feed/portfolio-stream-feed?update_types={update_types}"

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    break
                token = self._access_token_getter()
            if not token:
                with self._lock:
                    self._last_error = "no_token"
                    self._running = False
                break
            try:
                self._ws_app = websocket.WebSocketApp(
                    self._url(),
                    header=[f"Authorization: Bearer {token}", "Accept: application/json"],
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws_app.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)[:500]
                    self._connected = False
                logger.warning("Upstox portfolio stream failed: %s", exc)
            with self._lock:
                if not self._running:
                    break

    def _on_open(self, _ws: Any) -> None:
        with self._lock:
            self._connected = True
            self._last_error = None
        logger.info("Upstox portfolio stream connected")

    def _on_message(self, _ws: Any, message: Any) -> None:
        try:
            payload = json.loads(message) if isinstance(message, str) else {"raw": str(message)}
        except Exception:
            payload = {"raw": str(message)}
        with self._lock:
            self._events += 1
        self._event_callback(payload)

    def _on_error(self, _ws: Any, error: Any) -> None:
        with self._lock:
            self._last_error = str(error)[:500]
        logger.warning("Upstox portfolio stream error: %s", error)

    def _on_close(self, _ws: Any, status_code: Any, message: Any) -> None:
        with self._lock:
            self._connected = False
        logger.info("Upstox portfolio stream closed status=%s message=%s", status_code, message)
