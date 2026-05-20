"""Kotak Securities Neo V2 gateway for QuantG.

This module wraps the official Kotak Neo V2 SDK behind a small, stateful,
thread-safe gateway object. Credentials are read from either an explicit
configuration dictionary or environment variables; no secrets are hardcoded.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

try:  # pragma: no cover - depends on production dependency install
    import pyotp  # type: ignore
except Exception:  # pragma: no cover
    pyotp = None

try:  # pragma: no cover - depends on production dependency install
    from neo_api_client import NeoAPI  # type: ignore
except Exception:  # pragma: no cover
    NeoAPI = None


logger = logging.getLogger("quantg.kotak_gateway")


class QuantGNeoGateway:
    """Production-oriented Kotak Neo V2 client wrapper for QuantG.

    Expected configuration keys, either in ``config`` or environment:
    - ``consumer_key`` / ``KOTAK_CONSUMER_KEY``
    - ``mobile_number`` / ``KOTAK_MOBILE_NUMBER``
    - ``ucc`` / ``KOTAK_UCC``
    - ``mpin`` / ``KOTAK_MPIN``
    - ``totp_secret_key`` / ``KOTAK_TOTP_SECRET_KEY``
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, environment: str = "prod") -> None:
        """Create the gateway and initialize the private NeoAPI instance.

        The object keeps all session state internally so QuantG can reuse one
        authenticated gateway across order and streaming calls.
        """
        self._config = config or {}
        self._environment = self._get("environment", environment)
        self._consumer_key = self._get("consumer_key", env="KOTAK_CONSUMER_KEY")
        self._mobile_number = self._get("mobile_number", env="KOTAK_MOBILE_NUMBER")
        self._ucc = self._get("ucc", env="KOTAK_UCC")
        self._mpin = self._get("mpin", env="KOTAK_MPIN")
        self._totp_secret_key = self._get("totp_secret_key", env="KOTAK_TOTP_SECRET_KEY")

        self._client = None
        self._authenticated = False
        self._subscribed_tokens: List[Dict[str, str]] = []
        self._ticks_by_token: Dict[str, Dict[str, Any]] = {}
        self._ticks_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._orders_by_id: Dict[str, Dict[str, Any]] = {}
        self._last_error: Optional[str] = None
        self._lock = threading.RLock()

        if NeoAPI is None:
            self._set_error("neo_api_client is not installed")
            return
        if not self._consumer_key:
            self._set_error("KOTAK_CONSUMER_KEY is missing")
            return

        try:
            self._client = NeoAPI(environment=self._environment, consumer_key=self._consumer_key)
            self._install_callbacks()
        except Exception as exc:
            self._set_error(f"NeoAPI initialization failed: {exc}")

    def authenticate(self) -> Dict[str, Any]:
        """Run Kotak Neo two-factor login using TOTP and MPIN.

        Flow:
        1. Generate TOTP from the configured secret.
        2. Call ``totp_login(mobile_number, ucc, totp)``.
        3. Call the SDK's MPIN validation method to unlock the trade token.
        """
        if self._client is None:
            return self._failure("NeoAPI client is not initialized")
        missing = [
            name
            for name, value in {
                "mobile_number": self._mobile_number,
                "ucc": self._ucc,
                "mpin": self._mpin,
                "totp_secret_key": self._totp_secret_key,
            }.items()
            if not value
        ]
        if missing:
            return self._failure(f"Missing Kotak credentials: {', '.join(missing)}")
        if pyotp is None:
            return self._failure("pyotp is not installed")

        try:
            totp_code = pyotp.TOTP(str(self._totp_secret_key)).now()
            level_one = self._client.totp_login(
                mobile_number=str(self._mobile_number),
                ucc=str(self._ucc),
                totp=totp_code,
            )
            level_two = self._unlock_trade_session(str(self._mpin))
            with self._lock:
                self._authenticated = True
                self._last_error = None
            logger.info("Kotak Neo gateway authenticated for UCC=%s", self._mask(str(self._ucc)))
            return {"ok": True, "level_one": level_one, "level_two": level_two}
        except Exception as exc:
            return self._failure(f"Authentication failed: {exc}")

    def _unlock_trade_session(self, mpin: str) -> Any:
        """Complete Kotak's second-factor MPIN step across SDK variants."""
        if self._client is None:
            raise RuntimeError("NeoAPI client is not initialized")

        if hasattr(self._client, "totp_validate"):
            try:
                return self._client.totp_validate(mpin=mpin)
            except TypeError:
                return self._client.totp_validate(mpin)

        if hasattr(self._client, "password_login"):
            return self._client.password_login(password=mpin)

        login_methods = sorted(
            name for name in dir(self._client) if "login" in name.lower() or "validate" in name.lower()
        )
        raise RuntimeError(
            "Kotak Neo SDK MPIN validation method not found; available auth methods: "
            + ", ".join(login_methods or ["none"])
        )

    def place_order(
        self,
        *,
        exchange_segment: str,
        product: str,
        price: float,
        quantity: int,
        trading_symbol: str,
        transaction_type: str,
        order_type: str,
        validity: str = "DAY",
        disclosed_quantity: int = 0,
        trigger_price: float = 0,
        tag: str = "QuantG",
        **extra: Any,
    ) -> Dict[str, Any]:
        """Place an order through Kotak Neo using QuantG-normalized fields."""
        if self._client is None:
            return self._failure("NeoAPI client is not initialized")
        if not self._authenticated:
            return self._failure("Kotak Neo session is not authenticated")

        mapped = {
            "exchange_segment": exchange_segment,
            "product": product,
            "price": float(price or 0),
            "quantity": str(int(quantity)),
            "trading_symbol": trading_symbol,
            "transaction_type": transaction_type,
            "order_type": order_type,
            "validity": validity,
            "disclosed_quantity": str(int(disclosed_quantity or 0)),
            "trigger_price": float(trigger_price or 0),
            "tag": tag,
            **extra,
        }
        try:
            response = self._client.place_order(**mapped)
            logger.info("Kotak Neo order placed symbol=%s side=%s qty=%s", trading_symbol, transaction_type, quantity)
            return {"ok": True, "response": response}
        except Exception as exc:
            return self._failure(f"Order placement failed: {exc}")

    def subscribe_symbols(self, instrument_tokens: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Subscribe to market data ticks for Kotak token dictionaries.

        ``instrument_tokens`` must contain entries shaped like:
        ``{"instrument_token": "X", "exchange_segment": "Y"}``.
        """
        if self._client is None:
            return self._failure("NeoAPI client is not initialized")
        if not self._authenticated:
            return self._failure("Kotak Neo session is not authenticated")

        tokens = self._normalize_tokens(instrument_tokens)
        if not tokens:
            return self._failure("No valid instrument tokens supplied")

        try:
            self._install_callbacks()
            if hasattr(self._client, "subscribe"):
                response = self._client.subscribe(tokens)
            elif hasattr(self._client, "subscribe_symbol"):
                response = self._client.subscribe_symbol(tokens)
            else:
                return self._failure("NeoAPI subscribe method not found")
            with self._lock:
                self._subscribed_tokens = tokens
            logger.info("Kotak Neo subscribed to %s tokens", len(tokens))
            return {"ok": True, "tokens": tokens, "response": response}
        except Exception as exc:
            return self._failure(f"WebSocket subscribe failed: {exc}")

    def subscribe_order_feed(self) -> Dict[str, Any]:
        """Subscribe to Kotak's real-time order update feed."""
        if self._client is None:
            return self._failure("NeoAPI client is not initialized")
        if not self._authenticated:
            return self._failure("Kotak Neo session is not authenticated")
        try:
            self._install_callbacks()
            if hasattr(self._client, "subscribe_to_orderfeed"):
                response = self._client.subscribe_to_orderfeed()
            elif hasattr(self._client, "subscribeorderfeed"):
                response = self._client.subscribeorderfeed()
            else:
                return self._failure("NeoAPI order-feed subscribe method not found")
            logger.info("Kotak Neo order feed subscribed")
            return {"ok": True, "response": response}
        except Exception as exc:
            return self._failure(f"Order feed subscribe failed: {exc}")

    def on_message(self, message: Any) -> None:
        """SDK callback for market ticks; stores latest tick by token."""
        try:
            ticks = message if isinstance(message, list) else [message]
            received_at = datetime.now(timezone.utc).isoformat()
            with self._lock:
                for tick in ticks:
                    if not isinstance(tick, dict):
                        continue
                    token = str(tick.get("tk") or tick.get("instrument_token") or "")
                    ltp = tick.get("ltp") or tick.get("last_price") or tick.get("last_traded_price")
                    if token:
                        symbol = self._symbol_for_token(token)
                        normalized = {
                            "token": token,
                            "symbol": symbol,
                            "ltp": float(ltp) if ltp not in (None, "") else None,
                            "received_at": received_at,
                            "raw": tick,
                        }
                        self._ticks_by_token[token] = normalized
                        if symbol:
                            self._ticks_by_symbol[symbol.upper()] = normalized
                    order_id = str(tick.get("nOrdNo") or tick.get("order_id") or tick.get("orderNo") or "")
                    if order_id:
                        self._orders_by_id[order_id] = {
                            "order_id": order_id,
                            "status": tick.get("ordSt") or tick.get("status"),
                            "filled_qty": tick.get("fldQty") or tick.get("filled_quantity"),
                            "received_at": received_at,
                            "raw": tick,
                        }
                    if not token:
                        continue
                    if ltp not in (None, ""):
                        logger.debug("Kotak Neo tick token=%s ltp=%s", token, ltp)
        except Exception as exc:
            self._set_error(f"WebSocket message handling failed: {exc}")

    def on_error(self, error: Any) -> None:
        """SDK callback for streaming errors."""
        self._set_error(f"WebSocket error: {error}")

    def latest_tick(self, instrument_token: str) -> Optional[Dict[str, Any]]:
        """Return the latest normalized tick for one instrument token."""
        with self._lock:
            tick = self._ticks_by_token.get(str(instrument_token))
            return dict(tick) if tick else None

    def latest_tick_by_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Return the latest normalized tick for one subscribed symbol label."""
        with self._lock:
            tick = self._ticks_by_symbol.get(str(symbol).upper())
            return dict(tick) if tick else None

    def latest_ticks(self) -> Dict[str, Dict[str, Any]]:
        """Return a snapshot of all latest ticks keyed by token."""
        with self._lock:
            return {token: dict(tick) for token, tick in self._ticks_by_token.items()}

    def latest_orders(self) -> Dict[str, Dict[str, Any]]:
        """Return a snapshot of order-feed updates keyed by order id."""
        with self._lock:
            return {order_id: dict(order) for order_id, order in self._orders_by_id.items()}

    def status(self) -> Dict[str, Any]:
        """Return current gateway health and session metadata."""
        with self._lock:
            last_tick_at = None
            if self._ticks_by_token:
                last_tick_at = max(
                    (tick.get("received_at") for tick in self._ticks_by_token.values() if tick.get("received_at")),
                    default=None,
                )
            return {
                "initialized": self._client is not None,
                "authenticated": self._authenticated,
                "subscribed_tokens": len(self._subscribed_tokens),
                "ticks": len(self._ticks_by_token),
                "symbols": len(self._ticks_by_symbol),
                "order_updates": len(self._orders_by_id),
                "last_tick_at": last_tick_at,
                "last_error": self._last_error,
            }

    def logout(self) -> Dict[str, Any]:
        """Unsubscribe and tear down the active Kotak session/socket."""
        errors: List[str] = []
        if self._client is None:
            with self._lock:
                self._authenticated = False
                self._subscribed_tokens = []
            return {"ok": True, "message": "client_not_initialized"}

        try:
            with self._lock:
                tokens = list(self._subscribed_tokens)
            if tokens and hasattr(self._client, "un_subscribe"):
                self._client.un_subscribe(tokens)
            elif tokens and hasattr(self._client, "unsubscribe"):
                self._client.unsubscribe(tokens)
        except Exception as exc:
            msg = f"Unsubscribe failed: {exc}"
            errors.append(msg)
            self._set_error(msg)

        for method_name in ("close_connection", "close_websocket", "close", "logout"):
            try:
                method = getattr(self._client, method_name, None)
                if callable(method):
                    method()
                    break
            except Exception as exc:
                msg = f"{method_name} failed: {exc}"
                errors.append(msg)
                self._set_error(msg)

        with self._lock:
            self._authenticated = False
            self._subscribed_tokens = []
        return {"ok": not errors, "errors": errors}

    def _install_callbacks(self) -> None:
        if self._client is None:
            return
        try:
            setattr(self._client, "on_message", self.on_message)
            setattr(self._client, "on_error", self.on_error)
        except Exception as exc:
            self._set_error(f"Callback installation failed: {exc}")

    def _normalize_tokens(self, instrument_tokens: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
        out: List[Dict[str, str]] = []
        for item in instrument_tokens:
            token = item.get("instrument_token") if isinstance(item, dict) else None
            segment = item.get("exchange_segment") if isinstance(item, dict) else None
            if token and segment:
                normalized = {"instrument_token": str(token), "exchange_segment": str(segment)}
                symbol = item.get("symbol") if isinstance(item, dict) else None
                if symbol:
                    normalized["symbol"] = str(symbol).upper()
                out.append(normalized)
        return out

    def _symbol_for_token(self, token: str) -> Optional[str]:
        for item in self._subscribed_tokens:
            if str(item.get("instrument_token")) == str(token) and item.get("symbol"):
                return str(item["symbol"]).upper()
        return None

    def _get(self, key: str, default: Optional[str] = None, env: Optional[str] = None) -> Optional[str]:
        value = self._config.get(key)
        if value not in (None, ""):
            return str(value)
        if env:
            value = os.environ.get(env)
            if value not in (None, ""):
                return str(value)
        return default

    def _failure(self, message: str) -> Dict[str, Any]:
        self._set_error(message)
        return {"ok": False, "error": message}

    def _set_error(self, message: str) -> None:
        prefixed = f"QuantG Gateway Error: {message}"
        with self._lock:
            self._last_error = prefixed
        logger.error(prefixed)

    @staticmethod
    def _mask(value: str) -> str:
        if len(value) <= 4:
            return "*" * len(value)
        return f"{value[:2]}***{value[-2:]}"
