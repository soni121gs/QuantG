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

import kite_helper
from brokers.upstox_market_data_v3 import UpstoxMarketDataFeedV3


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
        sandbox: bool = False,
    ) -> None:
        import threading
        self.access_token = access_token or os.environ.get("UPSTOX_ACCESS_TOKEN")
        self.api_key = api_key or os.environ.get("UPSTOX_API_KEY")
        self.api_secret = api_secret or os.environ.get("UPSTOX_API_SECRET")
        self.redirect_uri = redirect_uri or os.environ.get("UPSTOX_REDIRECT_URI")
        self.timeout = float(timeout or os.environ.get("UPSTOX_TIMEOUT_SEC", "15"))
        self.last_error: Optional[str] = None
        self.last_request_at: Optional[str] = None
        self.last_order_response: Optional[Any] = None
        self.sandbox = sandbox or os.environ.get("UPSTOX_SANDBOX", "").lower() in ("true", "1", "yes")

        # Dynamic endpoints to support Sandbox vs Live
        self.api_base_url = "https://api-sandbox.upstox.com" if self.sandbox else "https://api.upstox.com"
        self.hft_base_url = "https://api-sandbox.upstox.com" if self.sandbox else "https://api-hft.upstox.com"
        self.auth_dialog_url = "https://api-sandbox.upstox.com/v2/login/authorization/dialog" if self.sandbox else "https://api.upstox.com/v2/login/authorization/dialog"
        self.token_url = "https://api-sandbox.upstox.com/v2/login/authorization/token" if self.sandbox else "https://api.upstox.com/v2/login/authorization/token"
        
        self._ticks_by_token: Dict[str, Dict[str, Any]] = {}
        self._ticks_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._subscribed_tokens: List[str] = []
        self._feed_v3 = UpstoxMarketDataFeedV3(
            access_token_getter=lambda: self.access_token,
            api_base_url=self.api_base_url,
            timeout=self.timeout,
        )
        self._ws_running = False
        self._ws_thread = None
        self._lock = threading.RLock()
        self.ticks = 0
        self.last_tick_at: Optional[str] = None

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
            "ticks": self.ticks,
            "last_tick_at": self.last_tick_at,
            "subscribed_tokens": len(self._subscribed_tokens),
            "ws_running": self._ws_running,
            "feed_status": self._feed_v3.status(),
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
        return f"{self.auth_dialog_url}?{urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: Optional[str] = None) -> Dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise ValueError("Upstox API key and secret are required")
        final_redirect_uri = redirect_uri or self.redirect_uri
        if not final_redirect_uri:
            raise ValueError("Upstox redirect URI is required")
        response = requests.post(
            self.token_url,
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
        payload = self._request("GET", "/v2/order/retrieve-all")
        items = order_items(payload)
        normalized = [kite_helper.normalize_order_update(item, broker="upstox") for item in items]
        return {"data": normalized, "orders": normalized, "raw": payload}

    def get_positions(self) -> Dict[str, Any]:
        payload = self._request("GET", "/v2/portfolio/short-term-positions")
        normalized = kite_helper.normalize_positions_payload(payload, broker="upstox")
        return {"data": normalized, "net": normalized.get("net") or [], "raw": payload}

    def get_margins(self) -> Dict[str, Any]:
        """Fetch Upstox user available and utilized funds across equity and commodity desks."""
        return self._request("GET", "/v2/user/get-funds-and-margin")

    def get_profile(self) -> Dict[str, Any]:
        """Validate the current Upstox access token with a lightweight user call."""
        return self._request("GET", "/v2/user/profile")

    def get_option_chain(self, underlying_key: str, expiry_date: str) -> Dict[str, Any]:
        """Fetch low-latency index Option Chain lookup from Upstox."""
        return self._request("GET", "/v2/option/chain", params={
            "instrument_key": underlying_key,
            "expiry_date": expiry_date,
        })

    def get_historical_candles(
        self,
        instrument_key: str,
        interval: str = "day",
        days: int = 60,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch historical candles from Upstox and return them in standard QuantG format."""
        from datetime import timedelta
        upstox_interval = interval
        if interval == "minute":
            upstox_interval = "1minute"
        elif interval == "5minute":
            upstox_interval = "1minute"  # Upstox doesn't support 5m; query 1m and resample!
        elif interval == "30minute":
            upstox_interval = "30minute"
            
        from urllib.parse import quote
        encoded_key = quote(instrument_key)
        now = datetime.now()
        to_date_str = now.strftime("%Y-%m-%d")
        from_date_str = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        if interval != "day":
            path = f"/v2/historical-candle/intraday/{encoded_key}/{upstox_interval}"
        else:
            path = f"/v2/historical-candle/{encoded_key}/{upstox_interval}/{to_date_str}/{from_date_str}"
        try:
            res = self._request("GET", path)
            if not isinstance(res, dict) or res.get("status") != "success":
                logger.warning(f"Upstox historical candles failed for {instrument_key}: {res}")
                return None
            
            data = res.get("data", {})
            candles = data.get("candles", []) or []
            
            # Upstox returns newest first; reverse to oldest first
            out = []
            for c in reversed(candles):
                if len(c) >= 5:
                    d_str = c[0]
                    if "T" in d_str:
                        date_val = d_str.split("+")[0].replace("T", " ")[:16]
                    else:
                        date_val = d_str
                    out.append({
                        "date": date_val,
                        "close": float(c[4] or 0),
                        "open": float(c[1] or 0),
                        "high": float(c[2] or 0),
                        "low": float(c[3] or 0),
                        "volume": int(c[5] or 0) if len(c) > 5 else 0,
                    })
            
            # Resample 1minute candles to 5minute candles if requested
            if interval == "5minute":
                resampled = []
                current_bar = None
                for c in out:
                    try:
                        dt_part, time_part = c["date"].split(" ")
                        hh, mm = time_part.split(":")[:2]
                        hh = int(hh)
                        mm = int(mm)
                        floored_mm = (mm // 5) * 5
                        bar_date = f"{dt_part} {hh:02d}:{floored_mm:02d}"
                    except Exception:
                        bar_date = c["date"]
                        
                    if current_bar is None or current_bar["date"] != bar_date:
                        if current_bar is not None:
                            resampled.append(current_bar)
                        current_bar = {
                            "date": bar_date,
                            "open": c["open"],
                            "high": c["high"],
                            "low": c["low"],
                            "close": c["close"],
                            "volume": c["volume"],
                        }
                    else:
                        current_bar["high"] = max(current_bar["high"], c["high"])
                        current_bar["low"] = min(current_bar["low"], c["low"])
                        current_bar["close"] = c["close"]
                        current_bar["volume"] += c["volume"]
                if current_bar is not None:
                    resampled.append(current_bar)
                out = resampled
                
            return out
        except Exception as e:
            logger.warning(f"Upstox historical candles failed for {instrument_key}: {e}")
            return None

    def search_instruments(
        self,
        query: str,
        *,
        exchanges: str = "ALL",
        segments: str = "ALL",
        instrument_types: Optional[str] = None,
        expiry: Optional[str] = None,
        atm_offset: Optional[int] = None,
        records: int = 20,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "query": str(query or "").strip()[:50],
            "exchanges": exchanges,
            "segments": segments,
            "page_number": 1,
            "records": max(1, min(int(records or 20), 30)),
        }
        if instrument_types:
            params["instrument_types"] = instrument_types
        if expiry:
            params["expiry"] = expiry
        if atm_offset is not None:
            params["atm_offset"] = int(atm_offset)
        return self._request("GET", "/v2/instruments/search", params=params)

    def get_market_quote(self, instrument_keys: Iterable[str]) -> Dict[str, Any]:
        keys = ",".join(str(k).strip() for k in instrument_keys if str(k).strip())
        if not keys:
            raise ValueError("At least one Upstox instrument key is required")
        res = self._request("GET", "/v2/market-quote/ltp", params={"instrument_key": keys})
        if isinstance(res, dict) and isinstance(res.get("data"), dict):
            received_at = datetime.now(timezone.utc).isoformat()
            with self._lock:
                self.ticks += len(res["data"])
                self.last_tick_at = received_at
                for token, node in res["data"].items():
                    if isinstance(node, dict):
                        ltp = node.get("last_price") or node.get("ltp")
                        if ltp not in (None, ""):
                            val = float(ltp)
                            symbol = token.split("|")[-1] if "|" in token else token
                            normalized = {
                                "token": token,
                                "instrument_key": token,
                                "symbol": symbol,
                                "exchange": token.split("|")[0].replace("_FO", "") if "|" in token else None,
                                "ltp": val,
                                "bid": node.get("bid_price") or node.get("bid") or node.get("bp"),
                                "ask": node.get("ask_price") or node.get("ask") or node.get("ap"),
                                "timestamp": node.get("last_trade_time") or node.get("timestamp") or received_at,
                                "timestamp_source": "broker_quote" if (node.get("last_trade_time") or node.get("timestamp")) else "server_received_at",
                                "received_at": received_at,
                                "raw": node,
                                "source": "REST",
                                "feed": "upstox-rest",
                            }
                            if normalized["timestamp_source"] == "server_received_at":
                                logger.warning("Upstox REST quote missing broker timestamp; using server received_at key=%s", token)
                            self._ticks_by_token[token] = normalized
                            self._ticks_by_symbol[symbol.upper()] = normalized
        return res

    @staticmethod
    def parse_quote_ltp(payload: Any, instrument_key: Optional[str] = None) -> Optional[float]:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if isinstance(data, dict):
            if instrument_key and instrument_key in data:
                node = data.get(instrument_key)
                if isinstance(node, dict):
                    for field in ("last_price", "ltp", "close", "last_traded_price"):
                        value = node.get(field)
                        if value not in (None, ""):
                            try:
                                return float(value)
                            except Exception:
                                pass
            for node in data.values():
                if isinstance(node, dict):
                    for field in ("last_price", "ltp", "close", "last_traded_price"):
                        value = node.get(field)
                        if value not in (None, ""):
                            try:
                                return float(value)
                            except Exception:
                                pass
        return None

    def latest_tick(self, instrument_token: str) -> Optional[Dict[str, Any]]:
        feed_tick = self._feed_v3.latest_tick(str(instrument_token))
        if feed_tick:
            return feed_tick
        with self._lock:
            tick = self._ticks_by_token.get(str(instrument_token))
            return dict(tick) if tick else None

    def latest_tick_by_symbol(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            tick = self._ticks_by_symbol.get(str(symbol).upper())
            return dict(tick) if tick else None

    def latest_ticks(self) -> Dict[str, Dict[str, Any]]:
        feed_ticks = self._feed_v3.latest_ticks()
        with self._lock:
            merged = {token: dict(tick) for token, tick in self._ticks_by_token.items()}
        merged.update(feed_ticks)
        return merged

    def start_market_data_ws(self, instruments: Iterable[str], mode: str = "ltpc") -> Dict[str, Any]:
        """Start real Upstox Market Data Feed V3 websocket tracking."""
        keys = [str(k).strip() for k in instruments if str(k).strip()]
        if not keys:
            logger.warning("Upstox V3 ticker startup skipped: no_instruments_supplied")
            return {"ok": False, "reason": "no_instruments_supplied"}
        if not self.access_token:
            self.last_error = "Ticker startup skipped: no_token"
            logger.warning("Upstox V3 ticker startup skipped: no_token instruments=%s", len(keys))
            return {"ok": False, "started": False, "reason": "no_token", "message": "Reconnect Upstox required"}
        with self._lock:
            current = set(self._subscribed_tokens)
            current.update(keys)
            self._subscribed_tokens = sorted(current)
            self._ws_running = True
        try:
            return self._feed_v3.subscribe(keys, mode=mode)
        except Exception as exc:
            self.last_error = f"Upstox V3 feed start failed: {exc}"
            logger.warning("%s", self.last_error)
            return {"ok": False, "reason": "feed_start_failed", "error": str(exc)}

    def stop_market_data_ws(self) -> Dict[str, Any]:
        with self._lock:
            self._ws_running = False
        return self._feed_v3.stop()

    def _request(self, method: str, path: str, *, hft: bool = False, **kwargs: Any) -> Dict[str, Any]:
        if not self.access_token:
            raise RuntimeError("Upstox access token is missing. Complete OAuth login first.")
        base = self.hft_base_url if hft else self.api_base_url
        headers = kwargs.pop("headers", {}) or {}
        algo_name = os.environ.get("UPSTOX_ALGO_NAME", "QuantGAlgo")
        headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "X-Algo-Name": algo_name,
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


def normalize_positions_response(payload: Any) -> Dict[str, List[Dict[str, Any]]]:
    return kite_helper.normalize_positions_payload(payload, broker="upstox")


def normalize_order_items(payload: Any) -> List[Dict[str, Any]]:
    return [kite_helper.normalize_order_update(item, broker="upstox") for item in order_items(payload)]


def position_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        out: List[Dict[str, Any]] = []
        for item in payload:
            out.extend(position_items(item))
        return out
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("net"), list):
        return [row for row in payload["net"] if isinstance(row, dict)]
    nested = payload.get("data")
    if isinstance(nested, dict) and isinstance(nested.get("net"), list):
        return [row for row in nested["net"] if isinstance(row, dict)]
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
