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

from brokers import normalization
from brokers.upstox_market_data_v3 import UpstoxMarketDataFeedV3


logger = logging.getLogger("quantg.upstox_gateway")

_last_warn_time: Dict[str, float] = {}


class UpstoxGateway:
    AUTH_DIALOG_URL = "https://api.upstox.com/v2/login/authorization/dialog"
    TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
    API_BASE_URL = "https://api.upstox.com"
    HFT_BASE_URL = "https://api-hft.upstox.com"

    def __init__(
        self,
        *,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
        timeout: Optional[float] = None,
        sandbox: bool = False,
    ) -> None:
        import threading
        self.access_token = access_token or os.environ.get("UPSTOX_ACCESS_TOKEN")
        self._refresh_token = refresh_token or os.environ.get("UPSTOX_REFRESH_TOKEN")
        self.api_key = api_key or os.environ.get("UPSTOX_API_KEY")
        self.api_secret = api_secret or os.environ.get("UPSTOX_API_SECRET")
        self.redirect_uri = redirect_uri or os.environ.get("UPSTOX_REDIRECT_URI")
        self.timeout = float(timeout or os.environ.get("UPSTOX_TIMEOUT_SEC", "15"))
        self.last_error: Optional[str] = None
        self.last_request_at: Optional[str] = None
        self.last_order_response: Optional[Any] = None
        self.sandbox = sandbox or os.environ.get("UPSTOX_SANDBOX", "").lower() in ("true", "1", "yes")

        # Track token refresh state
        self._token_refresh_count = 0
        self._token_refresh_last_error: Optional[str] = None
        self._token_refresh_last_at: Optional[str] = None

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
            refresh_token_callback=self._feed_auth_refresh,
        )
        self._ws_running = False
        self._ws_thread = None
        self._lock = threading.RLock()
        self.ticks = 0
        self.last_tick_at: Optional[str] = None

    def _feed_auth_refresh(self) -> bool:
        """Called by the feed when authorize fails — attempt to refresh the access token."""
        try:
            logger.info("Feed triggered token refresh for gateway")
            result = self.refresh_access_token()
            if result.get("ok"):
                logger.info("Feed token refreshed successfully via gateway")
                return True
            logger.warning("Feed token refresh failed via gateway: %s", result.get("error"))
            return False
        except Exception as exc:
            logger.warning("Feed token refresh exception: %s", exc)
            return False

    def refresh_access_token(self) -> Dict[str, Any]:
        """Refresh the Upstox access token using the stored refresh token."""
        if not self._refresh_token:
            self._token_refresh_last_error = "no_refresh_token"
            logger.warning("Upstox token refresh skipped: no refresh token available")
            return {"ok": False, "error": "no_refresh_token"}
        if not self.api_key or not self.api_secret:
            self._token_refresh_last_error = "missing_api_credentials"
            logger.warning("Upstox token refresh skipped: missing api_key/secret")
            return {"ok": False, "error": "missing_api_credentials"}
        try:
            response = requests.post(
                self.token_url,
                headers={"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "refresh_token": self._refresh_token,
                    "client_id": self.api_key,
                    "client_secret": self.api_secret,
                    "grant_type": "refresh_token",
                },
                timeout=self.timeout,
            )
            payload = self._decode_response(response)
            new_token = payload.get("access_token")
            if new_token:
                self.access_token = str(new_token)
                self._refresh_token = payload.get("refresh_token", self._refresh_token)
                self._token_refresh_count += 1
                self._token_refresh_last_error = None
                self._token_refresh_last_at = datetime.now(timezone.utc).isoformat()
                logger.info("Upstox access token refreshed successfully refresh_count=%s", self._token_refresh_count)
                return {
                    "ok": True,
                    "access_token": str(new_token),
                    "refresh_token": self._refresh_token,
                    "expires_in": payload.get("expires_in"),
                }
            self._token_refresh_last_error = "empty_token_in_response"
            return {"ok": False, "error": "empty_token_in_response", "payload": payload}
        except Exception as exc:
            self._token_refresh_last_error = str(exc)[:500]
            logger.warning("Upstox token refresh failed: %s", self._token_refresh_last_error)
            return {"ok": False, "error": self._token_refresh_last_error}

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
        feed_st = self._feed_v3.status()
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
            "subscribed_keys": list(self._subscribed_tokens)[:50],
            "ws_running": self._ws_running,
            "feed_status": feed_st,
            "refresh_token_present": bool(self._refresh_token),
            "token_refresh_count": self._token_refresh_count,
            "token_refresh_last_error": self._token_refresh_last_error,
            "token_refresh_last_at": self._token_refresh_last_at,
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
            # Also capture refresh_token if returned
            rt = payload.get("refresh_token")
            if rt:
                self._refresh_token = str(rt)
                logger.info("Upstox refresh_token captured from code exchange")
            else:
                logger.info("Upstox code exchange did not return refresh_token (token-only grant)")
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
        normalized = [normalization.normalize_order_update(item, broker="upstox") for item in items]
        return {"data": normalized, "orders": normalized, "raw": payload}

    def get_positions(self) -> Dict[str, Any]:
        payload = self._request("GET", "/v2/portfolio/short-term-positions")
        normalized = normalization.normalize_positions_payload(payload, broker="upstox")
        return {"data": normalized, "net": normalized.get("net") or [], "raw": payload}

    def get_margins(self) -> Dict[str, Any]:
        """Fetch Upstox user available and utilized funds across equity and commodity desks."""
        return self._request("GET", "/v2/user/get-funds-and-margin")

    def get_margin_details(self, instruments: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate upfront margin/premium requirements for proposed orders."""
        payload = {"instruments": list(instruments or [])}
        return self._request("POST", "/v2/charges/margin", json=payload)

    def get_brokerage_details(
        self,
        *,
        instrument_token: str,
        quantity: int,
        product: str,
        transaction_type: str,
        price: float,
    ) -> Dict[str, Any]:
        """Calculate Upstox brokerage and statutory charges for a proposed order."""
        return self._request(
            "GET",
            "/v2/charges/brokerage",
            params={
                "instrument_token": instrument_token,
                "quantity": int(quantity),
                "product": self.normalize_product(product),
                "transaction_type": str(transaction_type or "BUY").upper(),
                "price": float(price or 0),
            },
        )

    def get_profile(self) -> Dict[str, Any]:
        """Validate the current Upstox access token with a lightweight user call."""
        return self._request("GET", "/v2/user/profile")

    def get_option_chain(self, underlying_key: str, expiry_date: Optional[str] = None) -> Dict[str, Any]:
        """Fetch low-latency index Option Chain lookup from Upstox."""
        params = {"instrument_key": underlying_key}
        if expiry_date:
            params["expiry_date"] = expiry_date
        return self._request("GET", "/v2/option/chain", params=params)

    def get_option_contracts(self, underlying_key: str, expiry_date: Optional[str] = None) -> Dict[str, Any]:
        params = {"instrument_key": underlying_key}
        if expiry_date:
            params["expiry_date"] = expiry_date
        return self._request("GET", "/v2/option/contract", params=params)

    def place_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/v3/order/gtt/place", json=payload)

    def modify_gtt_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", "/v3/order/gtt/modify", json=payload)

    def cancel_gtt_order(self, gtt_order_id: str) -> Dict[str, Any]:
        return self._request("DELETE", "/v3/order/gtt/cancel", params={"gtt_order_id": gtt_order_id})

    def exit_all_positions(self, *, segment: Optional[str] = None, tag: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if segment:
            params["segment"] = segment
        if tag:
            params["tag"] = tag
        return self._request("POST", "/v2/order/positions/exit", params=params or None, json={})

    def get_historical_candles(
        self,
        instrument_key: str,
        interval: str = "day",
        days: int = 60,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch historical candles from Upstox in standard QuantG format.

        IMPORTANT: Upstox v2 historical-candle REST endpoint does NOT support index
        instrument keys (NSE_INDEX|*, BSE_INDEX|*).  For those we use the
        /v2/market-quote/ohlc endpoint which returns today's session OHLC for the
        index and synthesise intraday candles from it.  Daily bars are fetched via
        the normal historical-candle daily path which DOES work for index keys.
        """
        from urllib.parse import quote

        IS_INDEX = (
            instrument_key.startswith("NSE_INDEX|")
            or instrument_key.startswith("BSE_INDEX|")
        )

        # --- Index symbol path (uses market-quote/ohlc + daily candle endpoint) ---
        if IS_INDEX:
            return self._get_index_candles(instrument_key, interval, days)

        # --- Standard equity / derivative path ---------------------------------
        from datetime import timedelta

        # Intraday: the V2 /historical-candle/intraday endpoint returns ONLY the
        # current day, so early in the session there are too few bars (e.g. 14 by
        # 10:20) and callers requiring >=20 bars silently fall back to MOCK data.
        # Use the V3 multi-day historical endpoint (prior days) merged with today's
        # V3 intraday — the same approach the index path uses — so equity strategies
        # always get enough continuous REAL bars.
        if interval != "day":
            _v3_step = {"5minute": 5, "1minute": 1, "minute": 1,
                        "15minute": 15, "30minute": 30}.get(interval, 5)
            _now = datetime.now()
            _from = (_now - timedelta(days=max(days, 10))).strftime("%Y-%m-%d")
            _to = _now.strftime("%Y-%m-%d")
            _v3_hist: List[Dict[str, Any]] = []
            _v3_intra: List[Dict[str, Any]] = []
            try:
                _v3_hist = self.get_historical_candles_v3(
                    instrument_key, unit="minutes", interval=_v3_step,
                    from_date=_from, to_date=_to) or []
            except Exception as exc:
                logger.debug("Upstox V3 historical failed key=%s: %s", instrument_key, exc)
            try:
                _v3_intra = self.get_intraday_candles_v3(
                    instrument_key, unit="minutes", interval=_v3_step) or []
            except Exception as exc:
                logger.debug("Upstox V3 intraday failed key=%s: %s", instrument_key, exc)
            _merged = self._merge_candle_series(_v3_hist, _v3_intra)
            if _merged:
                return _merged
            # else: fall through to the legacy V2 intraday path below.

        upstox_interval = interval
        if interval == "minute":
            upstox_interval = "1minute"
        elif interval == "5minute":
            upstox_interval = "1minute"  # query 1m then resample to 5m below
        elif interval == "30minute":
            upstox_interval = "30minute"

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

            # Resample 1-minute candles to 5-minute candles if requested
            if interval == "5minute":
                out = self._resample_to_5min(out)

            return out
        except Exception as e:
            logger.warning(f"Upstox historical candles failed for {instrument_key}: {e}")
            return None

    def get_historical_candles_v3(
        self,
        instrument_key: str,
        *,
        unit: str = "minutes",
        interval: int = 1,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        from datetime import timedelta
        from urllib.parse import quote

        end = to_date or datetime.now().strftime("%Y-%m-%d")
        start = from_date or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        path = f"/v3/historical-candle/{quote(instrument_key)}/{unit}/{int(interval)}/{end}/{start}"
        return self._parse_candle_response(self._request("GET", path))

    def get_intraday_candles_v3(
        self,
        instrument_key: str,
        *,
        unit: str = "minutes",
        interval: int = 1,
    ) -> Optional[List[Dict[str, Any]]]:
        from urllib.parse import quote

        path = f"/v3/historical-candle/intraday/{quote(instrument_key)}/{unit}/{int(interval)}"
        return self._parse_candle_response(self._request("GET", path))

    def get_expired_historical_candles_v3(
        self,
        expired_instrument_key: str,
        *,
        unit: str = "minutes",
        interval: int = 1,
        from_date: str,
        to_date: str,
    ) -> Optional[List[Dict[str, Any]]]:
        from urllib.parse import quote

        path = f"/v3/expired-instruments/historical-candle/{quote(expired_instrument_key)}/{unit}/{int(interval)}/{to_date}/{from_date}"
        return self._parse_candle_response(self._request("GET", path))

    @staticmethod
    def _parse_candle_response(res: Any) -> Optional[List[Dict[str, Any]]]:
        if not isinstance(res, dict) or res.get("status") not in (None, "success"):
            return None
        candles = ((res.get("data") or {}).get("candles") if isinstance(res.get("data"), dict) else None) or []
        out: List[Dict[str, Any]] = []
        for c in reversed(candles):
            if len(c) >= 5:
                text = str(c[0])
                out.append({
                    "date": text.split("+")[0].replace("T", " ")[:16] if "T" in text else text,
                    "open": float(c[1] or 0),
                    "high": float(c[2] or 0),
                    "low": float(c[3] or 0),
                    "close": float(c[4] or 0),
                    "volume": int(c[5] or 0) if len(c) > 5 else 0,
                    "oi": float(c[6] or 0) if len(c) > 6 and c[6] not in (None, "") else 0.0,
                })
        return out

    def _get_index_candles(
        self,
        instrument_key: str,
        interval: str = "5minute",
        days: int = 60,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch candles for NSE/BSE index symbols.

        Upstox V2 /v2/historical-candle/intraday does NOT support index keys.
        Strategy:
          0. V3 historical multi-day 5-min (best: pure intraday data, 5+ days).
          1. Daily bars via the V2 daily endpoint (fallback context).
          2. Today's intraday 5-min bars via V3 intraday endpoint.
          3. Fallback: single-bar OHLC from /v2/market-quote/ohlc.
          4. Last resort: synthesise a full session from last daily close.
        """
        from datetime import timedelta
        from urllib.parse import quote

        OHLC_INTERVAL_MAP = {
            "1minute": "I1", "minute": "I1",
            "5minute": "I1", "15minute": "I1",
            "30minute": "I30", "60minute": "I1", "day": "1d",
        }
        ohlc_interval_code = OHLC_INTERVAL_MAP.get(interval, "I1")
        encoded_key = quote(instrument_key)

        # 0. V3 historical multi-day 5-min bars (primary for intraday strategies).
        # Returns pure consistent 5-min bars across multiple trading days so EMA/
        # SMA calculations are meaningful. Eliminates the need to mix daily bars
        # with intraday bars which produces wrong indicator values.
        if interval != "day":
            v3_hist_interval = {"5minute": 5, "1minute": 1, "minute": 1,
                                "15minute": 15, "30minute": 30}.get(interval, 5)
            try:
                hist_days = max(days, 10)
                hist_from = (datetime.now() - timedelta(days=hist_days)).strftime("%Y-%m-%d")
                hist_to = datetime.now().strftime("%Y-%m-%d")
                v3_hist_bars = self.get_historical_candles_v3(
                    instrument_key, unit="minutes", interval=v3_hist_interval,
                    from_date=hist_from, to_date=hist_to,
                )
                if v3_hist_bars and len(v3_hist_bars) >= 50:
                    logger.info(
                        "Upstox index V3 historical ok key=%s bars=%d interval=%dmin",
                        instrument_key, len(v3_hist_bars), v3_hist_interval,
                    )
                    return v3_hist_bars
            except Exception as exc:
                logger.debug("Upstox V3 historical failed key=%s: %s", instrument_key, exc)

        daily_bars: List[Dict[str, Any]] = []
        intraday_bars: List[Dict[str, Any]] = []

        # 1. Daily historical bars (fallback context when V3 historical unavailable)
        try:
            now = datetime.now()
            to_date = now.strftime("%Y-%m-%d")
            from_date = (now - timedelta(days=max(days, 30))).strftime("%Y-%m-%d")
            daily_path = f"/v2/historical-candle/{encoded_key}/day/{to_date}/{from_date}"
            res = self._request("GET", daily_path)
            if isinstance(res, dict) and res.get("status") == "success":
                candles = (res.get("data") or {}).get("candles") or []
                for c in reversed(candles):
                    if len(c) >= 5:
                        d_str = str(c[0])
                        date_val = d_str.split("T")[0] if "T" in d_str else d_str[:10]
                        daily_bars.append({
                            "date":   date_val,
                            "open":   float(c[1] or 0),
                            "high":   float(c[2] or 0),
                            "low":    float(c[3] or 0),
                            "close":  float(c[4] or 0),
                            "volume": int(c[5] or 0) if len(c) > 5 else 0,
                        })
                if daily_bars:
                    logger.debug(
                        "Upstox index daily candles ok key=%s count=%s",
                        instrument_key, len(daily_bars),
                    )
        except Exception as exc:
            logger.debug("Upstox index daily candle path failed key=%s: %s", instrument_key, exc)

        if interval == "day":
            return daily_bars if daily_bars else None

        # 2. V3 intraday 5-min bars (primary — real per-bar data for index instruments)
        v3_interval = {"5minute": 5, "1minute": 1, "minute": 1,
                       "15minute": 15, "30minute": 30}.get(interval, 5)
        try:
            v3_bars = self.get_intraday_candles_v3(
                instrument_key, unit="minutes", interval=v3_interval
            )
            if v3_bars and len(v3_bars) >= 1:
                intraday_bars = v3_bars
                logger.debug(
                    "Upstox index V3 intraday ok key=%s bars=%d interval=%dmin",
                    instrument_key, len(v3_bars), v3_interval,
                )
        except Exception as exc:
            logger.debug("Upstox V3 intraday failed key=%s: %s", instrument_key, exc)

        # 3. Fallback: single OHLC bar from market-quote/ohlc
        if not intraday_bars:
            try:
                ohlc_url = f"/v2/market-quote/ohlc?instrument_key={encoded_key}&interval={ohlc_interval_code}"
                res = self._request("GET", ohlc_url)
                if isinstance(res, dict) and res.get("status") == "success":
                    data_node = res.get("data") or {}
                    node = data_node.get(instrument_key) or (
                        next(iter(data_node.values()), {}) if data_node else {}
                    )
                    ohlc = node.get("ohlc") or {}
                    last_price = node.get("last_price") or node.get("ltp")
                    close_px = float(last_price or ohlc.get("close") or 0)
                    if close_px > 0:
                        ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
                        floored_min = (ist_now.minute // 5) * 5
                        bar_dt = ist_now.replace(minute=floored_min, second=0, microsecond=0)
                        bar_date_str = bar_dt.strftime("%Y-%m-%d %H:%M")
                        intraday_bars.append({
                            "date":   bar_date_str,
                            "open":   float(ohlc.get("open") or close_px),
                            "high":   float(ohlc.get("high") or close_px),
                            "low":    float(ohlc.get("low")  or close_px),
                            "close":  close_px,
                            "volume": int(node.get("volume") or node.get("total_buy_quantity") or 0),
                        })
                        logger.info(
                            "Upstox index OHLC ok key=%s ltp=%s bar=%s",
                            instrument_key, close_px, bar_date_str,
                        )
            except Exception as exc:
                logger.warning("Upstox index OHLC fetch failed key=%s: %s", instrument_key, exc)

        # 4. Last resort: synthesise full session from last daily close
        if not intraday_bars and daily_bars:
            last_close = daily_bars[-1]["close"]
            intraday_bars = self._synthesise_session_bars(last_close, interval)
            logger.info(
                "Upstox index: synthesised %s bars from daily close=%.2f key=%s",
                len(intraday_bars), last_close, instrument_key,
            )

        # 5. Merge: daily bars as context + today's real intraday bars
        today_str = (
            (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30))
            .strftime("%Y-%m-%d")
        )
        merged: List[Dict[str, Any]] = []
        for d in daily_bars:
            if d["date"] == today_str:
                continue  # replaced by intraday bars
            merged.append({
                "date":   f"{d['date']} 09:15",
                "open":   d["open"],
                "high":   d["high"],
                "low":    d["low"],
                "close":  d["close"],
                "volume": d["volume"],
            })
        merged.extend(intraday_bars)
        return merged if merged else None

    @staticmethod
    def _synthesise_session_bars(
        base_price: float,
        interval: str = "5minute",
        session_start: str = "09:15",
        session_end: str = "15:25",
    ) -> List[Dict[str, Any]]:
        """Generate a synthetic intraday session from a base price for index bootstrap."""
        import math as _math
        from datetime import timedelta as _td

        interval_minutes = {
            "1minute": 1, "minute": 1,
            "5minute": 5, "15minute": 15, "30minute": 30,
        }.get(interval, 5)
        h_s, m_s = map(int, session_start.split(":"))
        h_e, m_e = map(int, session_end.split(":"))
        today = (datetime.now(timezone.utc) + _td(hours=5, minutes=30)).date()
        bars: List[Dict[str, Any]] = []
        cur = datetime(today.year, today.month, today.day, h_s, m_s)
        end = datetime(today.year, today.month, today.day, h_e, m_e)
        price = base_price
        idx = 0
        while cur <= end:
            drift = _math.sin(idx / 8.0) * (base_price * 0.0008)
            bar_open  = round(price, 2)
            bar_close = round(price + drift, 2)
            bar_high  = round(max(bar_open, bar_close) * 1.0003, 2)
            bar_low   = round(min(bar_open, bar_close) * 0.9997, 2)
            bars.append({
                "date":   cur.strftime("%Y-%m-%d %H:%M"),
                "open":   bar_open,
                "high":   bar_high,
                "low":    bar_low,
                "close":  bar_close,
                "volume": 0,
            })
            price = bar_close
            cur += _td(minutes=interval_minutes)
            idx += 1
        return bars

    @staticmethod
    def _merge_candle_series(*series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Merge multiple oldest-first OHLCV bar lists into one continuous series.

        Dedupes by the bar "date" string (later series win on overlap, so today's
        intraday refreshes any same-timestamp historical bar) and returns the
        result oldest-first. The "YYYY-MM-DD HH:MM" date format sorts correctly
        lexicographically.
        """
        by_date: Dict[str, Dict[str, Any]] = {}
        for s in series:
            for bar in (s or []):
                d = bar.get("date")
                if d:
                    by_date[d] = bar
        return [by_date[d] for d in sorted(by_date.keys())]

    @staticmethod
    def _resample_to_5min(bars_1min: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Resample a list of 1-minute OHLCV bars to 5-minute bars."""
        resampled: List[Dict[str, Any]] = []
        current_bar: Optional[Dict[str, Any]] = None
        for c in bars_1min:
            try:
                dt_part, time_part = c["date"].split(" ")
                floored_mm = (int(time_part.split(":")[1]) // 5) * 5
                bar_date = f"{dt_part} {int(time_part.split(':')[0]):02d}:{floored_mm:02d}"
            except Exception:
                bar_date = c["date"]

            if current_bar is None or current_bar["date"] != bar_date:
                if current_bar is not None:
                    resampled.append(current_bar)
                current_bar = {
                    "date":   bar_date,
                    "open":   c["open"],
                    "high":   c["high"],
                    "low":    c["low"],
                    "close":  c["close"],
                    "volume": c["volume"],
                }
            else:
                current_bar["high"]   = max(current_bar["high"], c["high"])
                current_bar["low"]    = min(current_bar["low"],  c["low"])
                current_bar["close"]  = c["close"]
                current_bar["volume"] += c["volume"]
        if current_bar is not None:
            resampled.append(current_bar)
        return resampled

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
                                import time
                                now_ts = time.time()
                                last_time = _last_warn_time.get(token, 0.0)
                                if now_ts - last_time > 300:
                                    _last_warn_time[token] = now_ts
                                    logger.warning("Upstox REST quote missing broker timestamp; using server received_at key=%s (rate-limited)", token)
                            self._ticks_by_token[token] = normalized
                            self._ticks_by_symbol[symbol.upper()] = normalized
        return res

    @staticmethod
    def parse_quote_ltp(payload: Any, instrument_key: Optional[str] = None) -> Optional[float]:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if isinstance(data, dict):
            if instrument_key:
                if instrument_key in data:
                    node = data.get(instrument_key)
                    if isinstance(node, dict):
                        for field in ("last_price", "ltp", "close", "last_traded_price"):
                            value = node.get(field)
                            if value not in (None, ""):
                                try:
                                    return float(value)
                                except Exception:
                                    pass
                return None
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
        if str(self.access_token).startswith("mock_live_upstox_token"):
            self.last_error = "Ticker startup blocked: mock token in use"
            logger.warning("Upstox V3 ticker startup blocked: mock token detected")
            return {
                "ok": False,
                "started": False,
                "reason": "mock_token_blocked",
                "message": "Cannot start real market feed using a mock token. Reconnect live Upstox.",
            }
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

    def _handle_mock_request(self, method: str, path: str, kwargs: Any) -> Dict[str, Any]:
        path_lower = path.lower()
        method_upper = method.upper()
        
        # 1. Market Quote (LTP)
        if "/v2/market-quote/ltp" in path_lower or "/v2/market-quote/quotes" in path_lower:
            params = kwargs.get("params") or {}
            keys = params.get("instrument_key") or ""
            data = {}
            import math
            import time
            bucket = int(time.time() // 60)
            token_to_symbol = {
                "NSE_EQ|INE002A01018": "RELIANCE",
                "NSE_EQ|INE467B01029": "TCS",
                "NSE_EQ|INE040A01034": "HDFCBANK",
                "NSE_EQ|INE009A01021": "INFY",
                "NSE_EQ|INE090A01021": "ICICIBANK",
                "NSE_EQ|INE062A01020": "SBIN",
                "NSE_EQ|INE238A01034": "AXISBANK",
                "NSE_EQ|INE154A01025": "ITC",
                "NSE_EQ|INE018A01030": "LT",
                "NSE_EQ|INE585B01010": "MARUTI",
            }
            base_prices = {
                "RELIANCE": 2945.50, "TCS": 4120.20, "HDFCBANK": 1672.80,
                "INFY": 1890.45, "ICICIBANK": 1245.30, "SBIN": 824.10,
                "AXISBANK": 1180.60, "ITC": 482.95, "LT": 3680.55, "MARUTI": 12450.00
            }
            for key in keys.split(","):
                key = key.strip()
                if not key:
                    continue
                # spot indices
                if "Nifty 50" in key or ("NIFTY" in key and "INDEX" in key):
                    data[key] = {"last_price": 24850.40, "ltp": 24850.40}
                elif "Nifty Bank" in key or ("BANKNIFTY" in key and "INDEX" in key):
                    data[key] = {"last_price": 54000.00, "ltp": 54000.00}
                elif "SENSEX" in key:
                    data[key] = {"last_price": 81460.20, "ltp": 81460.20}
                # Option contracts / indices
                elif "54322" in key or "24850PE" in key or ("24850" in key and "PE" in key):
                    data[key] = {"last_price": 34.50, "ltp": 34.50}
                elif "54323" in key or "24850CE" in key or ("24850" in key and "CE" in key):
                    data[key] = {"last_price": 45.20, "ltp": 45.20}
                # Check mapping for mock equities
                elif key in token_to_symbol:
                    symbol = token_to_symbol[key]
                    base = base_prices[symbol]
                    seed = list(base_prices.keys()).index(symbol)
                    drift = math.sin(bucket / 11.0 + seed * 1.7) * (base * 0.004)
                    noise = math.sin(bucket / 5.0 + seed * 8.3) * (base * 0.001)
                    price = round(base + drift + noise, 2)
                    data[key] = {"last_price": price, "ltp": price}
                else:
                    data[key] = {"last_price": 34.50, "ltp": 34.50}
            return {"status": "success", "data": data}
            
        # 2. Option Chain
        if "/v2/option/chain" in path_lower:
            return {
                "status": "success",
                "data": [
                    {
                        "strike_price": 24800,
                        "expiry": "2026-06-04",
                        "call_options": {
                            "instrument_key": "NSE_FO|54320",
                            "trading_symbol": "NIFTY2660424800CE"
                        },
                        "put_options": {
                            "instrument_key": "NSE_FO|54321",
                            "trading_symbol": "NIFTY2660424800PE"
                        }
                    },
                    {
                        "strike_price": 24850,
                        "expiry": "2026-06-04",
                        "call_options": {
                            "instrument_key": "NSE_FO|54323",
                            "trading_symbol": "NIFTY2660424850CE"
                        },
                        "put_options": {
                            "instrument_key": "NSE_FO|54322",
                            "trading_symbol": "NIFTY2660424850PE"
                        }
                    },
                    {
                        "strike_price": 24900,
                        "expiry": "2026-06-04",
                        "call_options": {
                            "instrument_key": "NSE_FO|54324",
                            "trading_symbol": "NIFTY2660424900CE"
                        },
                        "put_options": {
                            "instrument_key": "NSE_FO|54325",
                            "trading_symbol": "NIFTY2660424900PE"
                        }
                    }
                ]
            }
            
        # 3. Instruments Search
        if "/v2/instruments/search" in path_lower:
            params = kwargs.get("params") or {}
            query = str(params.get("query") or "").upper()
            if "NIFTY" in query:
                return {
                    "status": "success",
                    "data": [
                        {
                            "instrument_key": "NSE_INDEX|Nifty 50",
                            "trading_symbol": "Nifty 50",
                            "exchange": "NSE",
                            "segment": "INDEX",
                            "instrument_type": "INDEX"
                        }
                    ]
                }
            return {"status": "success", "data": []}
            
        # 4. Place Order
        if "/v2/order/place" in path_lower:
            import json
            payload = kwargs.get("json") or {}
            logger.debug("Upstox API simulation order payload: %s", json.dumps(payload))
            
            logger.info(
                "Upstox Live Order Routed: instrument_token=%s quantity=%s side=%s order_type=%s product=%s validity=%s",
                payload.get("instrument_token"),
                payload.get("quantity"),
                payload.get("transaction_type"),
                payload.get("order_type"),
                payload.get("product"),
                payload.get("validity"),
            )
            return {"status": "success", "data": {"order_id": "260529123456"}}
            
        # 5. Profile
        if "/v2/user/profile" in path_lower:
            return {"status": "success", "data": {"email": "test_ops_user@quantg.io", "user_name": "Demo User"}}
            
        # 6. Funds and Margin
        if "/v2/user/get-funds-and-margin" in path_lower:
            return {
                "status": "success",
                "data": {
                    "equity": {"available_margin": 100000.0, "used_margin": 0.0},
                    "commodity": {"available_margin": 0.0, "used_margin": 0.0}
                }
            }
            
        # 7. Historical Candles
        if "/v2/historical-candle/" in path_lower:
            # Snap to IST %Y-%m-%d
            ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            return {
                "status": "success",
                "data": {
                    "candles": [
                        [ist_now.strftime("%Y-%m-%dT09:15:00+05:30"), 24800.0, 24850.0, 24780.0, 24820.0, 10000]
                    ]
                }
            }

        # 8. OHLC market quote (index spot fallback)
        if "/v2/market-quote/ohlc" in path_lower:
            return {
                "status": "success",
                "data": {
                    "NSE_INDEX|Nifty 50": {
                        "ohlc": {
                            "open": 24800.0,
                            "high": 24880.0,
                            "low": 24780.0,
                            "close": 24850.40
                        },
                        "last_price": 24850.40,
                        "volume": 50000
                    }
                }
            }
            
        return {"status": "success", "data": {}}

    def _request(self, method: str, path: str, *, hft: bool = False, _retry: int = 3, **kwargs: Any) -> Dict[str, Any]:
        import time as _time
        if not self.access_token:
            raise RuntimeError("Upstox access token is missing. Complete OAuth login first.")
        if str(self.access_token).startswith("mock_live_upstox_token") and "/v2/order/place" in path:
            raise RuntimeError("Upstox live order blocked: mock token cannot place production orders.")
        if str(self.access_token).startswith("mock_live_upstox_token"):
            return self._handle_mock_request(method, path, kwargs)
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
        last_exc: Optional[Exception] = None
        for attempt in range(max(1, _retry)):
            response = requests.request(
                method.upper(),
                f"{base}{path}",
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
            if response.status_code == 429:
                wait = min(int(response.headers.get("Retry-After", "1")), 60)
                logger.warning(
                    "Upstox rate-limited (429) on %s %s — backing off %ss (attempt %d/%d)",
                    method.upper(), path, wait, attempt + 1, _retry,
                )
                if attempt < _retry - 1:
                    _time.sleep(wait)
                    continue
                last_exc = RuntimeError(f"Upstox API 429: rate limit exceeded after {_retry} attempts on {path}")
                break
            return self._decode_response(response)
        raise last_exc or RuntimeError(f"Upstox API request failed after {_retry} attempts")

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
    return normalization.normalize_positions_payload(payload, broker="upstox")


def normalize_order_items(payload: Any) -> List[Dict[str, Any]]:
    return [normalization.normalize_order_update(item, broker="upstox") for item in order_items(payload)]


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
