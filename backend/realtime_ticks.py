from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional

try:
    from kiteconnect import KiteTicker
except ImportError:  # pragma: no cover
    KiteTicker = None

logger = logging.getLogger("quantg.realtime")

_TICK_INTERVAL_MINUTES = 5
_MAX_BARS = 400
_KITE_WS_ROOT = "wss://ws.kite.trade"


def _mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible:
        return "*" * len(value)
    return f"{value[:visible]}...{len(value)} chars"


def _auth_failure_text(value: Any) -> bool:
    text = str(value or "").lower()
    return any(part in text for part in ("403", "forbidden", "unauthor", "invalid token", "token"))


def _safe_ws_response(response: Any) -> Dict[str, Any]:
    if response is None:
        return {}
    out: Dict[str, Any] = {}
    for attr in ("peer", "protocol", "version", "headers"):
        try:
            val = getattr(response, attr, None)
            if val:
                out[attr] = val
        except Exception:
            pass
    if not out:
        out["repr"] = re.sub(r"access_token=[^&\s]+", "access_token=***", repr(response))
    return out


def _parse_timestamp(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            pass
    return datetime.now(timezone.utc)


def _bucket_time(dt: datetime, interval: int) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    minute = (dt.minute // interval) * interval
    return dt.replace(minute=minute)


class TickCandleBuilder:
    def __init__(self, interval_minutes: int = _TICK_INTERVAL_MINUTES, max_bars: int = _MAX_BARS):
        self.interval_minutes = interval_minutes
        self.max_bars = max_bars
        self._bars: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self.max_bars))
        self._current: Dict[str, Dict[str, Any]] = {}
        self._last_total_volume: Dict[str, int] = {}
        self._last_tick_at: Optional[datetime] = None

    def add_tick(self, symbol: str, price: float, volume: Optional[int], timestamp: Any) -> None:
        if price is None:
            return
        symbol = symbol.upper()
        ts = _parse_timestamp(timestamp)
        self._last_tick_at = ts
        bucket = _bucket_time(ts, self.interval_minutes)
        date = bucket.strftime("%Y-%m-%d %H:%M")
        bar = self._current.get(symbol)

        if bar is None or bar["date"] != date:
            if bar is not None:
                self._bars[symbol].append(bar)

            volume_delta = 0
            if volume is not None:
                previous = self._last_total_volume.get(symbol)
                if previous is not None:
                    volume_delta = max(0, volume - previous)
                else:
                    volume_delta = max(0, volume)
            self._current[symbol] = {
                "date": date,
                "open": float(price),
                "high": float(price),
                "low": float(price),
                "close": float(price),
                "volume": int(volume_delta),
            }
        else:
            bar["high"] = max(bar["high"], float(price))
            bar["low"] = min(bar["low"], float(price))
            bar["close"] = float(price)
            if volume is not None:
                previous = self._last_total_volume.get(symbol)
                if previous is not None:
                    delta = max(0, volume - previous)
                else:
                    delta = max(0, volume)
                bar["volume"] += int(delta)

        if volume is not None:
            self._last_total_volume[symbol] = int(volume)

    def get_candles(self, symbol: str, bars: int = 250) -> List[Dict[str, Any]]:
        symbol = symbol.upper()
        data = list(self._bars[symbol])
        current = self._current.get(symbol)
        if current is not None:
            data.append(current)
        return data[-bars:]

    def has_symbol(self, symbol: str) -> bool:
        symbol = symbol.upper()
        return bool(self._bars.get(symbol) or self._current.get(symbol))

    def last_tick_at(self) -> Optional[datetime]:
        return self._last_tick_at


class KiteRealtimeTicker:
    def __init__(self):
        self._ticker = None
        self._builder = TickCandleBuilder()
        self._token_to_symbol: Dict[int, str] = {}
        self._subscribed_tokens: List[int] = []
        self._lock = Lock()
        self._started = False
        self._last_connect_at: Optional[datetime] = None
        self._last_disconnect_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._connecting = False
        self._auth_failed = False
        self._reconnect_attempts = 0
        self._last_auth_response: Optional[Dict[str, Any]] = None

    def _parse_tick(self, tick: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        token = int(tick.get("instrument_token") or 0)
        symbol = self._token_to_symbol.get(token)
        if not symbol:
            return None
        price = tick.get("last_price") or tick.get("last_traded_price")
        if price is None:
            return None
        volume = None
        if tick.get("last_quantity") is not None:
            volume = int(tick["last_quantity"])
        elif tick.get("volume") is not None:
            volume = int(tick["volume"])
        return {
            "symbol": symbol,
            "price": float(price),
            "volume": volume,
            "timestamp": tick.get("timestamp") or tick.get("exchange_timestamp") or datetime.now(timezone.utc).isoformat(),
        }

    def _on_ticks(self, ws, ticks: List[Dict[str, Any]]) -> None:
        parsed_count = 0
        for tick in ticks:
            parsed = self._parse_tick(tick)
            if not parsed:
                continue
            parsed_count += 1
            self._builder.add_tick(
                parsed["symbol"],
                parsed["price"],
                parsed["volume"],
                parsed["timestamp"],
            )
        if parsed_count:
            sample = [
                {
                    "token": t.get("instrument_token"),
                    "symbol": self._token_to_symbol.get(int(t.get("instrument_token") or 0)),
                    "last_price": t.get("last_price") or t.get("last_traded_price"),
                }
                for t in ticks[:3]
            ]
            logger.info("Kite realtime incoming ticks count=%s sample=%s", parsed_count, sample)

    def _on_connect(self, ws, response: Any) -> None:
        self._last_connect_at = datetime.now(timezone.utc)
        self._last_error = None
        self._started = True
        self._connecting = False
        self._auth_failed = False
        self._reconnect_attempts = 0
        self._last_auth_response = _safe_ws_response(response)
        logger.info("Kite realtime websocket auth response: %s", self._last_auth_response)
        with self._lock:
            if not self._subscribed_tokens:
                return
            try:
                ws.subscribe(self._subscribed_tokens)
                ws.set_mode(ws.MODE_FULL, self._subscribed_tokens)
                logger.info(
                    "Kite realtime websocket connected root=%s subscribed_tokens=%s token_map=%s",
                    _KITE_WS_ROOT,
                    self._subscribed_tokens,
                    {t: self._token_to_symbol.get(t) for t in self._subscribed_tokens},
                )
            except Exception as e:
                logger.warning(f"Kite realtime subscribe failed: {e}")
                self._last_error = str(e)

    def _on_close(self, ws, code, reason) -> None:
        self._last_disconnect_at = datetime.now(timezone.utc)
        self._started = False
        self._connecting = False
        self._auth_failed = _auth_failure_text(code) or _auth_failure_text(reason)
        if self._auth_failed:
            self._last_error = f"auth_failed: {code}: {reason}; reconnect Zerodha on Broker Keys"
        logger.info(f"Kite realtime websocket closed: {code} {reason}")

    def _on_error(self, ws, code, reason) -> None:
        self._last_error = f"{code}: {reason}"
        self._started = False
        self._connecting = False
        self._auth_failed = _auth_failure_text(code) or _auth_failure_text(reason)
        if self._auth_failed:
            self._last_error = f"auth_failed: {self._last_error}; reconnect Zerodha on Broker Keys"
        logger.warning(f"Kite realtime websocket error: {code} {reason}")

    def _on_reconnect(self, ws, attempts_count: int) -> None:
        self._reconnect_attempts = attempts_count
        self._connecting = True
        logger.info("Kite realtime websocket reconnect attempt=%s", attempts_count)

    def _on_noreconnect(self, ws) -> None:
        self._connecting = False
        self._started = False
        self._last_error = self._last_error or "websocket reconnect attempts exhausted"
        logger.warning("Kite realtime websocket reconnect attempts exhausted")

    def start(self, api_key: str, access_token: str, token_to_symbol: Dict[int, str]) -> None:
        if KiteTicker is None:
            logger.warning("KiteRealtimeTicker cannot start because kiteconnect.KiteTicker is unavailable.")
            return

        with self._lock:
            if not token_to_symbol:
                return
            self._token_to_symbol.update(token_to_symbol)
            new_tokens = [t for t in token_to_symbol if t not in self._subscribed_tokens]
            self._subscribed_tokens = sorted({*self._subscribed_tokens, *token_to_symbol.keys()})

            if self._started and self._ticker is not None:
                try:
                    if new_tokens:
                        self._ticker.subscribe(new_tokens)
                        self._ticker.set_mode(self._ticker.MODE_FULL, self._subscribed_tokens)
                        logger.info(f"Kite realtime websocket updated subscriptions: {len(self._subscribed_tokens)} tokens")
                except Exception as e:
                    logger.warning(f"Kite realtime update subscriptions failed: {e}")
                    self._last_error = str(e)
                return

            if self._connecting and self._ticker is not None:
                return

            try:
                self._auth_failed = False
                self._reconnect_attempts = 0
                self._ticker = KiteTicker(
                    api_key,
                    access_token,
                    root=_KITE_WS_ROOT,
                    reconnect=True,
                    reconnect_max_tries=300,
                    reconnect_max_delay=10,
                )
                self._ticker.on_ticks = self._on_ticks
                self._ticker.on_connect = self._on_connect
                self._ticker.on_close = self._on_close
                self._ticker.on_error = self._on_error
                self._ticker.on_reconnect = self._on_reconnect
                self._ticker.on_noreconnect = self._on_noreconnect
                self._connecting = True
                self._ticker.connect(threaded=True)
                logger.info(
                    "Kite realtime ticker started root=%s api_key=%s access_token=%s subscribed_tokens=%s",
                    _KITE_WS_ROOT,
                    _mask_secret(api_key),
                    _mask_secret(access_token),
                    self._subscribed_tokens,
                )
            except Exception as e:
                self._ticker = None
                self._started = False
                self._connecting = False
                self._last_error = str(e)
                logger.warning(f"Failed to start Kite realtime ticker: {e}")

    def start_for_kite(self, kite, token_to_symbol: Dict[int, str]) -> None:
        api_key = getattr(kite, "api_key", None) or getattr(kite, "_api_key", None)
        access_token = getattr(kite, "access_token", None) or getattr(kite, "_access_token", None)
        if not api_key or not access_token:
            logger.warning("Unable to start realtime ticker: Kite instance missing api_key or access_token")
            return
        if not token_to_symbol:
            return
        self.start(api_key, access_token, token_to_symbol)

    def stop(self) -> None:
        with self._lock:
            if self._ticker is not None:
                try:
                    self._ticker.close()
                except Exception as e:
                    logger.warning(f"Error closing Kite realtime ticker: {e}")
                self._ticker = None
            self._started = False
            self._connecting = False

    def is_running(self) -> bool:
        return self._started

    def has_live_ticks(self) -> bool:
        return self._builder.last_tick_at() is not None

    def get_candles(self, symbol: str, bars: int = 250) -> List[Dict[str, Any]]:
        return self._builder.get_candles(symbol, bars)

    def has_symbol(self, symbol: str) -> bool:
        return self._builder.has_symbol(symbol)

    def status_info(self) -> Dict[str, Any]:
        return {
            "connected": self._started,
            "connecting": self._connecting,
            "subscribed_tokens": len(self._subscribed_tokens),
            "last_connect_at": self._last_connect_at.isoformat() if self._last_connect_at else None,
            "last_disconnect_at": self._last_disconnect_at.isoformat() if self._last_disconnect_at else None,
            "last_error": self._last_error,
            "last_tick_at": self._builder.last_tick_at().isoformat() if self._builder.last_tick_at() else None,
            "auth_failed": self._auth_failed,
            "reconnect_attempts": self._reconnect_attempts,
            "websocket_url": _KITE_WS_ROOT,
            "last_auth_response": self._last_auth_response,
        }


class RealtimeTickManager:
    def __init__(self):
        self._tickers: Dict[str, KiteRealtimeTicker] = {}
        self._lock = Lock()

    def start_for_user(self, user_id: str, kite, token_to_symbol: Dict[int, str]) -> None:
        with self._lock:
            if user_id not in self._tickers:
                self._tickers[user_id] = KiteRealtimeTicker()
            ticker = self._tickers[user_id]
        ticker.start_for_kite(kite, token_to_symbol)

    def stop(self) -> None:
        with self._lock:
            tickers = list(self._tickers.values())
        for ticker in tickers:
            ticker.stop()

    def is_running(self, user_id: str) -> bool:
        ticker = self._tickers.get(user_id)
        return ticker.is_running() if ticker else False

    def has_live_ticks(self, user_id: str) -> bool:
        ticker = self._tickers.get(user_id)
        return ticker.has_live_ticks() if ticker else False

    def has_symbol(self, user_id: str, symbol: str) -> bool:
        ticker = self._tickers.get(user_id)
        return ticker.has_symbol(symbol) if ticker else False

    def get_candles(self, user_id: str, symbol: str, bars: int = 250) -> List[Dict[str, Any]]:
        ticker = self._tickers.get(user_id)
        return ticker.get_candles(symbol, bars) if ticker else []

    def status_info(self, user_id: str) -> Dict[str, Any]:
        ticker = self._tickers.get(user_id)
        return ticker.status_info() if ticker else {"connected": False, "subscribed_tokens": 0, "last_connect_at": None, "last_disconnect_at": None, "last_error": None, "last_tick_at": None, "auth_failed": False, "reconnect_attempts": 0, "websocket_url": _KITE_WS_ROOT}
