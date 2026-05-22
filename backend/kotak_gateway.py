"""Kotak Securities Neo gateway for QuantG.

This module intentionally owns exactly one NeoAPI client per gateway instance.
The authenticated client is reused for orders, positions, search, quotes, and
websocket subscriptions; those methods never create fresh SDK clients.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from enum import Enum
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


class KotakGatewayState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CLIENT_CREATED = "CLIENT_CREATED"
    LOGIN_DONE = "LOGIN_DONE"
    TWO_FA_DONE = "TWO_FA_DONE"
    READY = "READY"
    FAILED = "FAILED"


class KotakNeoGateway:
    """Stateful Kotak Neo client wrapper.

    Required config/env:
    - consumer_key / KOTAK_CONSUMER_KEY
    - consumer_secret / KOTAK_CONSUMER_SECRET
    - mobile_number / KOTAK_MOBILE_NUMBER
    - password / KOTAK_PASSWORD, or mpin / KOTAK_MPIN
    - totp_secret_key / KOTAK_TOTP_SECRET_KEY
    - username/ucc is kept for display and compatibility, but login uses mobile.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, environment: str = "prod") -> None:
        self._config = config or {}
        self._environment = self._get("environment", environment) or "prod"
        self._consumer_key = self._get("consumer_key", env="KOTAK_CONSUMER_KEY")
        self._consumer_secret = self._get("consumer_secret", env="KOTAK_CONSUMER_SECRET")
        self._mobile_number = self._get("mobile_number", env="KOTAK_MOBILE_NUMBER")
        self._username = self._get("username", env="KOTAK_USERNAME") or self._get("ucc", env="KOTAK_UCC")
        self._password = self._get("password", env="KOTAK_PASSWORD") or self._get("mpin", env="KOTAK_MPIN")
        self._totp_secret_key = self._get("totp_secret_key", env="KOTAK_TOTP_SECRET_KEY")

        self._client = None
        self._state = KotakGatewayState.DISCONNECTED
        self._authenticated = False
        self._subscribed_tokens: List[Dict[str, str]] = []
        self._ticks_by_token: Dict[str, Dict[str, Any]] = {}
        self._ticks_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._orders_by_id: Dict[str, Dict[str, Any]] = {}
        self._last_error: Optional[str] = None
        self._last_login_at: Optional[str] = None
        self._last_ready_at: Optional[str] = None
        self._last_order_request: Optional[Dict[str, Any]] = None
        self._last_order_response: Optional[Any] = None
        self._last_successful_order_at: Optional[str] = None
        self._lock = threading.RLock()

        self._create_client_once()

    def _create_client_once(self) -> None:
        if NeoAPI is None:
            self._set_error("neo_api_client is not installed in this backend image")
            return
        missing = [
            name
            for name, value in {
                "consumer_key": self._consumer_key,
                "consumer_secret": self._consumer_secret,
            }.items()
            if not value
        ]
        if missing:
            self._set_error(f"Missing Kotak API credentials: {', '.join(missing)}")
            return
        with self._lock:
            self._last_error = None
            self._state = KotakGatewayState.DISCONNECTED
        try:
            logger.info("Kotak Neo creating SDK client environment=%s", self._environment)
            self._client = self._build_sdk_client()
            self._transition(KotakGatewayState.CLIENT_CREATED, "NeoAPI client created")
            self._install_callbacks()
        except Exception as exc:
            self._set_error(f"NeoAPI initialization failed: {self._friendly_error(exc)}")

    def _build_sdk_client(self):
        """Create NeoAPI once, tolerating small SDK constructor differences."""
        attempts = [
            {
                "consumer_key": str(self._consumer_key),
                "consumer_secret": str(self._consumer_secret),
                "environment": self._environment,
                "access_token": None,
            },
            {
                "consumer_key": str(self._consumer_key),
                "consumer_secret": str(self._consumer_secret),
                "environment": self._environment,
            },
        ]
        errors: List[str] = []
        for kwargs in attempts:
            try:
                return NeoAPI(**kwargs)
            except TypeError as exc:
                errors.append(self._friendly_error(exc))
                continue
        raise RuntimeError("; ".join(errors) or "NeoAPI constructor rejected QuantG credentials")

    def authenticate(self) -> Dict[str, Any]:
        """Run login -> TOTP -> session_2fa before any trading/data call."""
        if self._client is None:
            return self._failure("NeoAPI client is not initialized. Check SDK install and consumer credentials.")
        missing = [
            name
            for name, value in {
                "mobile_number": self._mobile_number,
                "password_or_mpin": self._password,
                "totp_secret_key": self._totp_secret_key,
            }.items()
            if not value
        ]
        if missing:
            return self._failure(f"Missing Kotak login credentials: {', '.join(missing)}")
        if pyotp is None:
            return self._failure("pyotp is not installed")

        try:
            logger.info("Kotak Neo login step 1 started mobile=%s", self._mask(str(self._mobile_number)))
            login_response = self._client.login(
                mobilenumber=str(self._mobile_number),
                password=str(self._password),
            )
            login_error = self._response_error(login_response)
            if login_error:
                return self._failure(f"Kotak login failed: {login_error}")
            self._transition(KotakGatewayState.LOGIN_DONE, "Login API completed")
            with self._lock:
                self._last_login_at = datetime.now(timezone.utc).isoformat()

            otp = pyotp.TOTP(str(self._totp_secret_key)).now()
            logger.info("Kotak Neo login step 2FA started")
            two_fa_response = self._client.session_2fa(OTP=otp)
            two_fa_error = self._response_error(two_fa_response)
            if two_fa_error:
                return self._failure(f"Kotak 2FA failed: {two_fa_error}")
            self._transition(KotakGatewayState.TWO_FA_DONE, "Session 2FA completed")

            with self._lock:
                self._authenticated = True
                self._last_error = None
                self._last_ready_at = datetime.now(timezone.utc).isoformat()
            self._transition(KotakGatewayState.READY, "Gateway ready")
            logger.info("Kotak Neo gateway authenticated username=%s", self._mask(str(self._username or "")))
            return {"ok": True, "login": login_response, "session_2fa": two_fa_response, "status": self.status()}
        except Exception as exc:
            return self._failure(f"Authentication failed: {self._friendly_error(exc)}")

    def reconnect(self) -> Dict[str, Any]:
        """Safely re-run login on the existing SDK client after session expiry."""
        if self._client is None:
            previous_error = self._last_error
            self._create_client_once()
        if self._client is None:
            return {
                "ok": False,
                "error": self._last_error or previous_error or "Kotak Neo SDK client could not be created. Check Consumer Key, Consumer Secret, SDK install, and backend logs.",
                "state": self._state.value,
            }
        logger.info("Kotak Neo reconnect requested current_state=%s", self._state.value)
        with self._lock:
            self._authenticated = False
            self._subscribed_tokens = []
            self._state = KotakGatewayState.CLIENT_CREATED
        return self.authenticate()

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
        blocked = self._require_ready("place_order")
        if blocked:
            return blocked
        mapped = {
            "exchange_segment": exchange_segment,
            "product": product,
            "price": str(price or 0),
            "quantity": str(int(quantity)),
            "trading_symbol": trading_symbol,
            "transaction_type": transaction_type,
            "order_type": order_type,
            "validity": validity,
            "amo": "NO",
            "disclosed_quantity": str(int(disclosed_quantity or 0)),
            "market_protection": "0",
            "pf": "N",
            "trigger_price": str(trigger_price or 0),
            "tag": tag,
            **extra,
        }
        with self._lock:
            self._last_order_request = dict(mapped)
            self._last_order_response = None
        try:
            response = self._client.place_order(**mapped)
            with self._lock:
                self._last_order_response = self._compact(response)
            response_error = self._response_error(response)
            if response_error:
                return self._failure(f"Order placement failed: {response_error}")
            logger.info("Kotak Neo order placed symbol=%s side=%s qty=%s", trading_symbol, transaction_type, quantity)
            with self._lock:
                self._last_successful_order_at = datetime.now(timezone.utc).isoformat()
                self._last_error = None
            return {"ok": True, "response": response}
        except Exception as exc:
            return self._failure(f"Order placement failed: {self._friendly_error(exc)}")

    def order_report(self) -> Dict[str, Any]:
        blocked = self._require_ready("order_report")
        if blocked:
            return blocked
        try:
            if not hasattr(self._client, "order_report"):
                return self._failure("NeoAPI order_report method not found")
            response = self._client.order_report()
            response_error = self._response_error(response)
            if response_error:
                return self._failure(f"Order report failed: {response_error}")
            return {"ok": True, "response": response}
        except Exception as exc:
            return self._failure(f"Order report failed: {self._friendly_error(exc)}")

    def positions(self) -> Dict[str, Any]:
        blocked = self._require_ready("positions")
        if blocked:
            return blocked
        try:
            if not hasattr(self._client, "positions"):
                return self._failure("NeoAPI positions method not found")
            response = self._client.positions()
            response_error = self._response_error(response)
            if response_error:
                return self._failure(f"Positions fetch failed: {response_error}")
            return {"ok": True, "response": response}
        except Exception as exc:
            return self._failure(f"Positions fetch failed: {self._friendly_error(exc)}")

    def search_scrip(
        self,
        *,
        exchange_segment: str,
        symbol: str = "",
        expiry: Optional[str] = None,
        option_type: Optional[str] = None,
        strike_price: Optional[str] = None,
    ) -> Dict[str, Any]:
        blocked = self._require_ready("search_scrip")
        if blocked:
            return blocked
        try:
            if not hasattr(self._client, "search_scrip"):
                return self._failure("NeoAPI search_scrip method not found")
            response = self._client.search_scrip(
                exchange_segment=exchange_segment,
                symbol=symbol,
                expiry=expiry,
                option_type=option_type,
                strike_price=strike_price,
            )
            response_error = self._response_error(response)
            if response_error:
                return self._failure(f"Scrip search failed: {response_error}")
            return {"ok": True, "response": response}
        except Exception as exc:
            return self._failure(f"Scrip search failed: {self._friendly_error(exc)}")

    def quotes(self, instrument_tokens: Iterable[Dict[str, Any]], quote_type: str = "ltp") -> Dict[str, Any]:
        blocked = self._require_ready("quotes")
        if blocked:
            return blocked
        tokens = self._normalize_tokens(instrument_tokens)
        if not tokens:
            return self._failure("No valid instrument tokens supplied")
        try:
            if not hasattr(self._client, "quotes"):
                return self._failure("NeoAPI quotes method not found")
            response = self._client.quotes(instrument_tokens=tokens, quote_type=quote_type)
            response_error = self._response_error(response)
            if response_error:
                return self._failure(f"Quotes failed: {response_error}")
            self._ingest_quote_response(response)
            return {"ok": True, "response": response}
        except Exception as exc:
            return self._failure(f"Quotes failed: {self._friendly_error(exc)}")

    def subscribe_symbols(self, instrument_tokens: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        blocked = self._require_ready("subscribe_symbols")
        if blocked:
            return blocked
        tokens = self._normalize_tokens(instrument_tokens)
        if not tokens:
            return self._failure("No valid instrument tokens supplied")
        try:
            self._install_callbacks()
            if hasattr(self._client, "subscribe"):
                response = self._client.subscribe(
                    instrument_tokens=tokens,
                    isIndex=any(str(t.get("exchange_segment", "")).endswith("_idx") for t in tokens),
                    isDepth=False,
                )
            elif hasattr(self._client, "subscribe_symbol"):
                response = self._client.subscribe_symbol(tokens)
            else:
                return self._failure("NeoAPI subscribe method not found")
            response_error = self._response_error(response)
            if response_error:
                return self._failure(f"WebSocket subscribe failed: {response_error}")
            with self._lock:
                self._subscribed_tokens = tokens
            self._seed_latest_quotes(tokens)
            logger.info("Kotak Neo subscribed to %s tokens", len(tokens))
            return {"ok": True, "tokens": tokens, "response": response}
        except Exception as exc:
            return self._failure(f"WebSocket subscribe failed: {self._friendly_error(exc)}")

    def subscribe_order_feed(self) -> Dict[str, Any]:
        blocked = self._require_ready("subscribe_order_feed")
        if blocked:
            return blocked
        try:
            self._install_callbacks()
            if hasattr(self._client, "subscribe_to_orderfeed"):
                response = self._client.subscribe_to_orderfeed()
            elif hasattr(self._client, "subscribeorderfeed"):
                response = self._client.subscribeorderfeed()
            else:
                return self._failure("NeoAPI order-feed subscribe method not found")
            response_error = self._response_error(response)
            if response_error:
                return self._failure(f"Order feed subscribe failed: {response_error}")
            logger.info("Kotak Neo order feed subscribed")
            return {"ok": True, "response": response}
        except Exception as exc:
            return self._failure(f"Order feed subscribe failed: {self._friendly_error(exc)}")

    def on_message(self, message: Any) -> None:
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
        except Exception as exc:
            self._set_error(f"WebSocket message handling failed: {self._friendly_error(exc)}")

    def on_error(self, error: Any) -> None:
        self._set_error(f"WebSocket error: {self._friendly_error(error)}")

    def latest_tick(self, instrument_token: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            tick = self._ticks_by_token.get(str(instrument_token))
            return dict(tick) if tick else None

    def latest_tick_by_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            tick = self._ticks_by_symbol.get(str(symbol).upper())
            return dict(tick) if tick else None

    def latest_ticks(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {token: dict(tick) for token, tick in self._ticks_by_token.items()}

    def latest_orders(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {order_id: dict(order) for order_id, order in self._orders_by_id.items()}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            last_tick_at = None
            if self._ticks_by_token:
                last_tick_at = max(
                    (tick.get("received_at") for tick in self._ticks_by_token.values() if tick.get("received_at")),
                    default=None,
                )
            return {
                "initialized": self._client is not None,
                "state": self._state.value,
                "authenticated": self._state == KotakGatewayState.READY and self._authenticated,
                "ready": self._state == KotakGatewayState.READY,
                "subscribed_tokens": len(self._subscribed_tokens),
                "ticks": len(self._ticks_by_token),
                "symbols": len(self._ticks_by_symbol),
                "order_updates": len(self._orders_by_id),
                "last_tick_at": last_tick_at,
                "last_error": self._last_error,
                "last_login_at": self._last_login_at,
                "last_ready_at": self._last_ready_at,
                "last_order_request": dict(self._last_order_request) if self._last_order_request else None,
                "last_order_response": self._last_order_response,
                "last_successful_order_at": self._last_successful_order_at,
            }

    def logout(self) -> Dict[str, Any]:
        errors: List[str] = []
        if self._client is None:
            with self._lock:
                self._authenticated = False
                self._subscribed_tokens = []
                self._state = KotakGatewayState.DISCONNECTED
            return {"ok": True, "message": "client_not_initialized"}
        try:
            with self._lock:
                tokens = list(self._subscribed_tokens)
            if tokens and hasattr(self._client, "un_subscribe"):
                self._client.un_subscribe(tokens)
            elif tokens and hasattr(self._client, "unsubscribe"):
                self._client.unsubscribe(tokens)
        except Exception as exc:
            msg = f"Unsubscribe failed: {self._friendly_error(exc)}"
            errors.append(msg)
            logger.warning(msg)
        for method_name in ("close_connection", "close_websocket", "close", "logout"):
            try:
                method = getattr(self._client, method_name, None)
                if callable(method):
                    method()
                    break
            except Exception as exc:
                msg = f"{method_name} failed: {self._friendly_error(exc)}"
                errors.append(msg)
                logger.warning(msg)
        with self._lock:
            self._authenticated = False
            self._subscribed_tokens = []
            self._state = KotakGatewayState.DISCONNECTED
        return {"ok": not errors, "errors": errors}

    def _require_ready(self, operation: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            ready = self._state == KotakGatewayState.READY and self._authenticated and self._client is not None
            state = self._state.value
            last_error = self._last_error
        if ready:
            return None
        return {
            "ok": False,
            "error": (
                f"Kotak Neo is not ready for {operation}. Current state is {state}. "
                f"Click Connect Kotak to complete login + 2FA first."
                + (f" Last error: {last_error}" if last_error else "")
            ),
            "state": state,
        }

    def _install_callbacks(self) -> None:
        if self._client is None:
            return
        try:
            setattr(self._client, "on_message", self.on_message)
            setattr(self._client, "on_error", self.on_error)
        except Exception as exc:
            self._set_error(f"Callback installation failed: {self._friendly_error(exc)}")

    def _seed_latest_quotes(self, tokens: List[Dict[str, str]]) -> None:
        if self._client is None or not tokens or not hasattr(self._client, "quotes"):
            return
        try:
            response = self._client.quotes(instrument_tokens=tokens, quote_type="ltp")
            self._ingest_quote_response(response)
        except Exception as exc:
            logger.warning("Kotak quote seed failed: %s", self._friendly_error(exc))

    def _ingest_quote_response(self, response: Any) -> None:
        received_at = datetime.now(timezone.utc).isoformat()
        candidates: List[Dict[str, Any]] = []

        def collect(node: Any) -> None:
            if isinstance(node, list):
                for item in node:
                    collect(item)
                return
            if not isinstance(node, dict):
                return
            token = node.get("tk") or node.get("instrument_token") or node.get("instrumentToken") or node.get("token")
            ltp = node.get("ltp") or node.get("last_price") or node.get("lastPrice") or node.get("last_traded_price")
            if token and ltp not in (None, ""):
                candidates.append({"token": str(token), "ltp": ltp, "raw": node})
            for value in node.values():
                if isinstance(value, (dict, list)):
                    collect(value)

        collect(response)
        if not candidates:
            return
        with self._lock:
            for item in candidates:
                symbol = self._symbol_for_token(item["token"])
                normalized = {
                    "token": item["token"],
                    "symbol": symbol,
                    "ltp": float(item["ltp"]),
                    "received_at": received_at,
                    "raw": item["raw"],
                }
                self._ticks_by_token[item["token"]] = normalized
                if symbol:
                    self._ticks_by_symbol[symbol.upper()] = normalized

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

    def _transition(self, state: KotakGatewayState, message: str) -> None:
        with self._lock:
            self._state = state
        logger.info("Kotak Neo state=%s: %s", state.value, message)

    def _failure(self, message: str) -> Dict[str, Any]:
        self._set_error(message)
        return {"ok": False, "error": self._last_error or message, "state": self._state.value}

    def _set_error(self, message: str) -> None:
        readable = self._friendly_error(message)
        prefixed = f"QuantG Gateway Error: {readable}"
        with self._lock:
            self._last_error = prefixed
            self._authenticated = False
            self._state = KotakGatewayState.FAILED
        logger.error(prefixed)

    def _response_error(self, payload: Any) -> Optional[str]:
        if payload is None:
            return None
        if isinstance(payload, dict):
            for key in ("Error", "Error Message", "error", "errMsg", "errorMessage", "message", "Message"):
                value = payload.get(key)
                if value not in (None, "", [], {}):
                    return self._friendly_error(value)
            status = str(payload.get("stat") or payload.get("status") or payload.get("State") or "").upper()
            if status in {"NOT_OK", "FAIL", "FAILED", "ERROR", "REJECTED"}:
                return self._friendly_error(payload.get("reason") or payload)
            for value in payload.values():
                found = self._response_error(value)
                if found:
                    return found
        if isinstance(payload, list):
            for item in payload:
                found = self._response_error(item)
                if found:
                    return found
        return None

    def _friendly_error(self, value: Any) -> str:
        text = self._compact_text(value)
        lower = text.lower()
        if "static ip" in lower or "ip whitelist" in lower or "whitelist" in lower:
            return "Kotak rejected the request because this server IP is not whitelisted for your Trade API subscription. Add the VPS static IP in Kotak Neo API settings, then reconnect."
        if "subscription" in lower or "subscribed" in lower or "trade api" in lower:
            return "Kotak Trade API subscription is not active for this account or key. Activate/renew the Kotak Neo Trade API subscription, then reconnect."
        if "complete the 2fa" in lower or "2fa" in lower:
            return "Kotak session is not fully authenticated. Click Connect Kotak again to complete login and TOTP 2FA."
        if "token" in lower and ("expired" in lower or "invalid" in lower):
            return "Kotak session token expired or is invalid. Reconnect Kotak."
        if "neo api client is not initialized" in lower:
            return "Kotak Neo SDK client was not created. Check Consumer Key, Consumer Secret, SDK install, and backend logs."
        return text[:1000]

    def _compact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(k): self._compact(v) for k, v in list(value.items())[:50]}
        if isinstance(value, list):
            return [self._compact(v) for v in value[:20]]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)

    def _compact_text(self, value: Any) -> str:
        text = str(self._compact(value))
        return text[:1000]

    @staticmethod
    def _mask(value: str) -> str:
        if not value:
            return ""
        if len(value) <= 4:
            return "*" * len(value)
        return f"{value[:2]}***{value[-2:]}"


# Backwards-compatible import name used by older QuantG modules.
QuantGNeoGateway = KotakNeoGateway
