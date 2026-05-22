"""Upstox API v2 gateway for QuantG.

The gateway intentionally uses plain HTTPS requests. That keeps the adapter
stable even when the optional SDK changes its generated method names.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode

import requests


logger = logging.getLogger("quantg.upstox_gateway")


class UpstoxGateway:
    AUTH_DIALOG_URL = "https://api.upstox.com/v2/login/authorization/dialog"
    TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
    API_BASE_URL = "https://api.upstox.com"
    HFT_BASE_URL = "https://api-hft.upstox.com"

    def __init__(
        self,
        *,
        access_token: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.access_token = access_token or os.environ.get("UPSTOX_ACCESS_TOKEN")
        self.api_key = api_key or os.environ.get("UPSTOX_API_KEY")
        self.api_secret = api_secret or os.environ.get("UPSTOX_API_SECRET")
        self.redirect_uri = redirect_uri or os.environ.get("UPSTOX_REDIRECT_URI")
        self.timeout = float(timeout or os.environ.get("UPSTOX_TIMEOUT_SEC", "15"))
        self.last_error: Optional[str] = None
        self.last_request_at: Optional[str] = None
        self.last_order_response: Optional[Any] = None

    @property
    def connected(self) -> bool:
        return bool(self.access_token)

    def status(self) -> Dict[str, Any]:
        missing = [
            name
            for name, value in {
                "UPSTOX_API_KEY": self.api_key,
                "UPSTOX_API_SECRET": self.api_secret,
                "UPSTOX_REDIRECT_URI": self.redirect_uri,
            }.items()
            if not value
        ]
        return {
            "connected": self.connected,
            "authenticated": self.connected,
            "keys_ready": bool(self.api_key and self.api_secret),
            "redirect_uri_ready": bool(self.redirect_uri),
            "missing_env": missing,
            "last_error": self.last_error,
            "last_request_at": self.last_request_at,
        }

    def build_login_url(self, *, state: Optional[str] = None, redirect_uri: Optional[str] = None) -> str:
        if not self.api_key:
            raise ValueError("UPSTOX_API_KEY/api_key is required")
        final_redirect_uri = redirect_uri or self.redirect_uri
        if not final_redirect_uri:
            raise ValueError("UPSTOX_REDIRECT_URI/redirect_uri is required")
        params = {
            "response_type": "code",
            "client_id": self.api_key,
            "redirect_uri": final_redirect_uri,
        }
        if state:
            params["state"] = state
        return f"{self.AUTH_DIALOG_URL}?{urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise ValueError("Upstox API key and secret are required")
        final_redirect_uri = redirect_uri or self.redirect_uri
        if not final_redirect_uri:
            raise ValueError("Upstox redirect URI is required")
        response = requests.post(
            self.TOKEN_URL,
            headers={"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": code,
                "client_id": self.api_key,
                "client_secret": self.api_secret,
                "redirect_uri": final_redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=self.timeout,
        )
        payload = self._decode_response(response)
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if token:
            self.access_token = str(token)
        return payload

    def place_order(
        self,
        *,
        instrument_token: str,
        quantity: int,
        side: str,
        order_type: str = "MARKET",
        product: str = "I",
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        validity: str = "DAY",
        disclosed_quantity: int = 0,
        is_amo: bool = False,
        market_protection: Optional[float] = -1,
        tag: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        payload = {
            "quantity": int(quantity),
            "product": self.normalize_product(product),
            "validity": (validity or "DAY").upper(),
            "price": float(price or 0),
            "tag": tag or "",
            "instrument_token": instrument_token,
            "order_type": (order_type or "MARKET").upper(),
            "transaction_type": (side or "BUY").upper(),
            "disclosed_quantity": int(disclosed_quantity or 0),
            "trigger_price": float(trigger_price or 0),
            "is_amo": bool(is_amo),
            "market_protection": -1 if market_protection is None else market_protection,
            **extra,
        }
        result = self._request("POST", "/v2/order/place", hft=True, json=payload)
        self.last_order_response = result
        return result

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return self._request("DELETE", "/v2/order/cancel", hft=True, params={"order_id": order_id})

    def get_order_book(self) -> Dict[str, Any]:
        return self._request("GET", "/v2/order/retrieve-all")

    def get_positions(self) -> Dict[str, Any]:
        return self._request("GET", "/v2/portfolio/short-term-positions")

    def get_market_quote(self, instrument_keys: Iterable[str]) -> Dict[str, Any]:
        keys = ",".join(str(k).strip() for k in instrument_keys if str(k).strip())
        if not keys:
            raise ValueError("At least one Upstox instrument key is required")
        return self._request("GET", "/v2/market-quote/ltp", params={"instrument_key": keys})

    def start_market_data_ws(self, instruments: Iterable[str]) -> Dict[str, Any]:
        keys = [str(k).strip() for k in instruments if str(k).strip()]
        return {
            "ok": False,
            "started": False,
            "reason": "upstox_websocket_not_enabled",
            "message": "REST adapter is ready. Wire Upstox websocket authorization/feed decoding after live token tests.",
            "instruments": keys,
        }

    def _request(self, method: str, path: str, *, hft: bool = False, **kwargs: Any) -> Dict[str, Any]:
        if not self.access_token:
            raise RuntimeError("Upstox access token is missing. Complete OAuth login first.")
        base = self.HFT_BASE_URL if hft else self.API_BASE_URL
        headers = kwargs.pop("headers", {}) or {}
        headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        })
        self.last_request_at = datetime.now(timezone.utc).isoformat()
        response = requests.request(
            method.upper(),
            f"{base}{path}",
            headers=headers,
            timeout=self.timeout,
            **kwargs,
        )
        return self._decode_response(response)

    def _decode_response(self, response: requests.Response) -> Dict[str, Any]:
        try:
            payload: Any = response.json()
        except Exception:
            payload = {"raw": response.text}
        if response.status_code >= 400:
            self.last_error = self._friendly_error(payload)
            raise RuntimeError(f"Upstox API {response.status_code}: {self.last_error}")
        self.last_error = None
        return payload if isinstance(payload, dict) else {"data": payload}

    @staticmethod
    def normalize_product(product: Optional[str]) -> str:
        value = (product or "I").upper()
        return {
            "MIS": "I",
            "INTRADAY": "I",
            "I": "I",
            "CNC": "D",
            "DELIVERY": "D",
            "NRML": "D",
            "NORMAL": "D",
            "D": "D",
            "CO": "CO",
            "MTF": "MTF",
        }.get(value, value)

    @staticmethod
    def _friendly_error(payload: Any) -> str:
        if isinstance(payload, dict):
            for key in ("message", "error", "errors", "status"):
                value = payload.get(key)
                if value not in (None, "", [], {}):
                    return str(value)[:1000]
        return str(payload)[:1000]


def extract_order_id(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("order_id", "orderId"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        for value in payload.values():
            found = extract_order_id(value)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = extract_order_id(item)
            if found:
                return found
    return None


def order_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        out: List[Dict[str, Any]] = []
        for item in payload:
            out.extend(order_items(item))
        return out
    if not isinstance(payload, dict):
        return []
    if extract_order_id(payload):
        return [payload]
    out: List[Dict[str, Any]] = []
    for key in ("data", "orders", "orderBook", "order_book", "result", "records"):
        if key in payload:
            out.extend(order_items(payload.get(key)))
    if out:
        return out
    for value in payload.values():
        out.extend(order_items(value))
    return out


def position_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        out: List[Dict[str, Any]] = []
        for item in payload:
            out.extend(position_items(item))
        return out
    if not isinstance(payload, dict):
        return []
    if any(key in payload for key in ("quantity", "net_quantity", "overnight_quantity")) and any(
        key in payload for key in ("instrument_token", "tradingsymbol", "trading_symbol")
    ):
        return [payload]
    out: List[Dict[str, Any]] = []
    for key in ("data", "positions", "positionBook", "position_book", "result", "records"):
        if key in payload:
            out.extend(position_items(payload.get(key)))
    if out:
        return out
    for value in payload.values():
        out.extend(position_items(value))
    return out

