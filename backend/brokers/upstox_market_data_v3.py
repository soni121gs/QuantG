"""Upstox Market Data Feed V3 websocket client.

This module keeps the live feed separate from order/REST concerns. Upstox V3
uses a websocket subscription frame and protobuf messages for market ticks.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional

import requests
from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory

try:
    import websocket
except Exception:  # pragma: no cover - exercised by runtime environments without websocket-client
    websocket = None


logger = logging.getLogger("quantg.upstox_feed_v3")

SUPPORTED_MODES = {"ltpc", "full", "option_chain", "option_greeks", "full_d30"}


def _build_feed_response_class():
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "MarketDataFeedV3.proto"
    file_proto.package = "com.upstox.marketdatafeederv3udapi.rpc.proto"
    file_proto.syntax = "proto3"

    def add_message(name: str, fields: list[tuple[str, int, int, Optional[str], bool]]) -> None:
        msg = file_proto.message_type.add()
        msg.name = name
        for field_name, number, field_type, type_name, repeated in fields:
            field = msg.field.add()
            field.name = field_name
            field.number = number
            field.label = (
                descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED
                if repeated
                else descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
            )
            field.type = field_type
            if type_name:
                field.type_name = type_name

    def add_oneof_message(name: str, oneof_name: str, fields: list[tuple[str, int, str]]) -> None:
        msg = file_proto.message_type.add()
        msg.name = name
        msg.oneof_decl.add().name = oneof_name
        for field_name, number, type_name in fields:
            field = msg.field.add()
            field.name = field_name
            field.number = number
            field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
            field.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
            field.type_name = type_name
            field.oneof_index = 0

    def add_enum(name: str, values: list[tuple[str, int]]) -> None:
        enum = file_proto.enum_type.add()
        enum.name = name
        for value_name, value_number in values:
            value = enum.value.add()
            value.name = value_name
            value.number = value_number

    add_message("LTPC", [
        ("ltp", 1, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("ltt", 2, descriptor_pb2.FieldDescriptorProto.TYPE_INT64, None, False),
        ("ltq", 3, descriptor_pb2.FieldDescriptorProto.TYPE_INT64, None, False),
        ("cp", 4, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
    ])
    add_message("Quote", [
        ("bidQ", 1, descriptor_pb2.FieldDescriptorProto.TYPE_INT64, None, False),
        ("bidP", 2, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("askQ", 3, descriptor_pb2.FieldDescriptorProto.TYPE_INT64, None, False),
        ("askP", 4, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
    ])
    add_message("MarketLevel", [
        ("bidAskQuote", 1, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.Quote", True),
    ])
    add_message("OHLC", [
        ("interval", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING, None, False),
        ("open", 2, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("high", 3, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("low", 4, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("close", 5, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("vol", 6, descriptor_pb2.FieldDescriptorProto.TYPE_INT64, None, False),
        ("ts", 7, descriptor_pb2.FieldDescriptorProto.TYPE_INT64, None, False),
    ])
    add_message("MarketOHLC", [
        ("ohlc", 1, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.OHLC", True),
    ])
    add_message("OptionGreeks", [
        ("delta", 1, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("theta", 2, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("gamma", 3, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("vega", 4, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("rho", 5, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
    ])
    add_enum("Type", [("initial_feed", 0), ("live_feed", 1), ("market_info", 2)])
    add_enum("RequestMode", [("ltpc", 0), ("full_d5", 1), ("option_greeks", 2), ("full_d30", 3)])
    add_enum("MarketStatus", [
        ("PRE_OPEN_START", 0), ("PRE_OPEN_END", 1), ("NORMAL_OPEN", 2),
        ("NORMAL_CLOSE", 3), ("CLOSING_START", 4), ("CLOSING_END", 5),
    ])
    add_message("MarketFullFeed", [
        ("ltpc", 1, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.LTPC", False),
        ("marketLevel", 2, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.MarketLevel", False),
        ("optionGreeks", 3, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.OptionGreeks", False),
        ("marketOHLC", 4, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.MarketOHLC", False),
        ("atp", 5, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("vtt", 6, descriptor_pb2.FieldDescriptorProto.TYPE_INT64, None, False),
        ("oi", 7, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("iv", 8, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("tbq", 9, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("tsq", 10, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
    ])
    add_message("IndexFullFeed", [
        ("ltpc", 1, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.LTPC", False),
        ("marketOHLC", 2, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.MarketOHLC", False),
    ])
    add_oneof_message("FullFeed", "FullFeedUnion", [
        ("marketFF", 1, ".com.upstox.marketdatafeederv3udapi.rpc.proto.MarketFullFeed"),
        ("indexFF", 2, ".com.upstox.marketdatafeederv3udapi.rpc.proto.IndexFullFeed"),
    ])
    add_message("FirstLevelWithGreeks", [
        ("ltpc", 1, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.LTPC", False),
        ("firstDepth", 2, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.Quote", False),
        ("optionGreeks", 3, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.OptionGreeks", False),
        ("vtt", 4, descriptor_pb2.FieldDescriptorProto.TYPE_INT64, None, False),
        ("oi", 5, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
        ("iv", 6, descriptor_pb2.FieldDescriptorProto.TYPE_DOUBLE, None, False),
    ])
    msg = file_proto.message_type.add()
    msg.name = "Feed"
    msg.oneof_decl.add().name = "FeedUnion"
    for field_name, number, type_name in [
        ("ltpc", 1, ".com.upstox.marketdatafeederv3udapi.rpc.proto.LTPC"),
        ("fullFeed", 2, ".com.upstox.marketdatafeederv3udapi.rpc.proto.FullFeed"),
        ("firstLevelWithGreeks", 3, ".com.upstox.marketdatafeederv3udapi.rpc.proto.FirstLevelWithGreeks"),
    ]:
        field = msg.field.add()
        field.name = field_name
        field.number = number
        field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
        field.type = descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE
        field.type_name = type_name
        field.oneof_index = 0
    field = msg.field.add()
    field.name = "requestMode"
    field.number = 4
    field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
    field.type = descriptor_pb2.FieldDescriptorProto.TYPE_ENUM
    field.type_name = ".com.upstox.marketdatafeederv3udapi.rpc.proto.RequestMode"

    market_entry = file_proto.message_type.add()
    market_entry.name = "MarketInfo_SegmentStatusEntry"
    market_entry.options.map_entry = True
    for field_name, number, field_type, type_name in [
        ("key", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING, None),
        ("value", 2, descriptor_pb2.FieldDescriptorProto.TYPE_ENUM, ".com.upstox.marketdatafeederv3udapi.rpc.proto.MarketStatus"),
    ]:
        field = market_entry.field.add()
        field.name = field_name
        field.number = number
        field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
        field.type = field_type
        if type_name:
            field.type_name = type_name
    add_message("MarketInfo", [
        ("segmentStatus", 1, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.MarketInfo_SegmentStatusEntry", True),
    ])

    feeds_entry = file_proto.message_type.add()
    feeds_entry.name = "FeedResponse_FeedsEntry"
    feeds_entry.options.map_entry = True
    for field_name, number, field_type, type_name in [
        ("key", 1, descriptor_pb2.FieldDescriptorProto.TYPE_STRING, None),
        ("value", 2, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.Feed"),
    ]:
        field = feeds_entry.field.add()
        field.name = field_name
        field.number = number
        field.label = descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL
        field.type = field_type
        if type_name:
            field.type_name = type_name
    add_message("FeedResponse", [
        ("type", 1, descriptor_pb2.FieldDescriptorProto.TYPE_ENUM, ".com.upstox.marketdatafeederv3udapi.rpc.proto.Type", False),
        ("feeds", 2, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.FeedResponse_FeedsEntry", True),
        ("currentTs", 3, descriptor_pb2.FieldDescriptorProto.TYPE_INT64, None, False),
        ("marketInfo", 4, descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE, ".com.upstox.marketdatafeederv3udapi.rpc.proto.MarketInfo", False),
    ])

    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_proto)
    descriptor = pool.FindMessageTypeByName("com.upstox.marketdatafeederv3udapi.rpc.proto.FeedResponse")
    try:
        return message_factory.GetMessageClass(descriptor)
    except AttributeError:
        return message_factory.MessageFactory(pool).GetPrototype(descriptor)


FeedResponse = _build_feed_response_class()


def build_subscription_payload(instrument_keys: Iterable[str], mode: str = "ltpc", method: str = "sub") -> bytes:
    keys = sorted({str(key).strip() for key in instrument_keys if str(key).strip()})
    if not keys:
        raise ValueError("At least one Upstox instrument_key is required")
    final_mode = "option_greeks" if mode == "option_chain" else (mode or "ltpc")
    if final_mode not in SUPPORTED_MODES:
        raise ValueError(f"Unsupported Upstox feed mode: {mode}")
    if final_mode == "full":
        final_mode = "full_d5"
    payload = {
        "guid": uuid.uuid4().hex,
        "method": method,
        "data": {
            "mode": final_mode,
            "instrumentKeys": keys,
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


_last_warn_time: Dict[str, float] = {}

_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_JITTER_FACTOR = 0.25
_RECONNECT_MAX_CONSECUTIVE_FAILURES = 50


def decode_feed_response(raw: bytes) -> Dict[str, Any]:
    message = FeedResponse()
    message.ParseFromString(raw)
    return json_format.MessageToDict(message, preserving_proto_field_name=True)


def extract_ltp_tick(instrument_key: str, feed: Dict[str, Any], *, current_ts: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(feed, dict):
        return None
    ltpc = feed.get("ltpc")
    
    # Try nested fullFeed / full_feed
    if not ltpc:
        full_feed = feed.get("fullFeed") or feed.get("full_feed")
        if isinstance(full_feed, dict):
            market_ff = full_feed.get("marketFF") or full_feed.get("market_ff")
            if isinstance(market_ff, dict):
                ltpc = market_ff.get("ltpc")
            if not ltpc:
                index_ff = full_feed.get("indexFF") or full_feed.get("index_ff")
                if isinstance(index_ff, dict):
                    ltpc = index_ff.get("ltpc")
                    
    # Try nested firstLevelWithGreeks / first_level_with_greeks
    if not ltpc:
        first_level = feed.get("firstLevelWithGreeks") or feed.get("first_level_with_greeks")
        if isinstance(first_level, dict):
            ltpc = first_level.get("ltpc")
            
    if not isinstance(ltpc, dict):
        return None
    ltp = ltpc.get("ltp")
    if ltp in (None, ""):
        return None
    received_at = datetime.now(timezone.utc).isoformat()
    broker_ts = ltpc.get("ltt") or current_ts
    timestamp_source = "broker_ltt" if ltpc.get("ltt") else "upstox_current_ts" if current_ts else "server_received_at"
    if timestamp_source == "server_received_at":
        now_ts = time.time()
        last_time = _last_warn_time.get(instrument_key, 0.0)
        if now_ts - last_time > 300:
            _last_warn_time[instrument_key] = now_ts
            logger.warning("Upstox V3 tick missing broker timestamp; using server received_at key=%s (rate-limited)", instrument_key)
    return {
        "token": instrument_key,
        "instrument_key": instrument_key,
        "symbol": instrument_key.split("|")[-1] if "|" in instrument_key else instrument_key,
        "exchange": instrument_key.split("|")[0].replace("_FO", "") if "|" in instrument_key else None,
        "ltp": float(ltp),
        "bid": None,
        "ask": None,
        "close": float(ltpc.get("cp") or 0),
        "last_trade_time": broker_ts,
        "timestamp": broker_ts or received_at,
        "timestamp_source": timestamp_source,
        "last_trade_quantity": ltpc.get("ltq"),
        "received_at": received_at,
        "exchange_ts": current_ts,
        "raw": feed,
        "source": "websocket",
        "feed": "upstox-market-data-feed-v3",
    }


class UpstoxMarketDataFeedV3:
    def __init__(
        self,
        *,
        access_token_getter: Callable[[], Optional[str]],
        api_base_url: str,
        timeout: float = 15,
        refresh_token_callback: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._access_token_getter = access_token_getter
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = timeout
        self._refresh_token_callback = refresh_token_callback
        self._lock = threading.RLock()
        self._subscribed: Dict[str, str] = {}
        self._ticks: Dict[str, Dict[str, Any]] = {}
        self._ws_app: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._state = "disconnected"
        self._last_error: Optional[str] = None
        self._last_tick_time: Optional[str] = None
        self._reconnects = 0
        self._consecutive_failures = 0
        self._first_tick_logged = False

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "connected": self._connected,
                "state": self._state,
                "last_error": self._last_error,
                "last_tick_time": self._last_tick_time,
                "subscribed_count": len(self._subscribed),
                "reconnects": self._reconnects,
                "feed": "upstox-market-data-feed-v3",
            }

    def latest_tick(self, instrument_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            tick = self._ticks.get(str(instrument_key))
            return dict(tick) if tick else None

    def latest_ticks(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {key: dict(value) for key, value in self._ticks.items()}

    def subscribe(self, instrument_keys: Iterable[str], mode: str = "ltpc") -> Dict[str, Any]:
        keys = [str(key).strip() for key in instrument_keys if str(key).strip()]
        if not keys:
            return {"ok": False, "reason": "no_instruments_supplied"}
        
        mode = mode or "ltpc"
        to_subscribe = []
        skipped_count = 0
        
        with self._lock:
            for key in keys:
                if self._subscribed.get(key) == mode:
                    skipped_count += 1
                else:
                    self._subscribed[key] = mode
                    to_subscribe.append(key)
            
            if to_subscribe:
                if self._connected and self._ws_app and self._ws_app.sock:
                    self._send_subscription_locked(to_subscribe, mode)
                logger.info(
                    "Upstox V3 subscription sent mode=%s instrument_count=%s (skipped=%s duplicate), total_subscribed=%s",
                    mode, len(to_subscribe), skipped_count, len(self._subscribed)
                )
            else:
                if skipped_count > 0:
                    logger.debug(
                        "Upstox V3 subscription skipped (all %s instruments were duplicates), total_subscribed=%s",
                        skipped_count, len(self._subscribed)
                    )
            
            if not self._running:
                self._running = True
                self._state = "reconnecting"
                self._thread = threading.Thread(target=self._run_forever, name="upstox-feed-v3", daemon=True)
                self._thread.start()
        return {"ok": True, "started": True, "tokens": len(self._subscribed), "feed": "upstox-market-data-feed-v3"}

    def stop(self) -> Dict[str, Any]:
        with self._lock:
            self._running = False
            self._state = "disconnected"
            self._subscribed.clear()
            self._ticks.clear()
            app = self._ws_app
        if app:
            try:
                app.close()
            except Exception:
                pass
        return {"ok": True, "stopped": True}

    def authorize_url(self) -> str:
        token = self._access_token_getter()
        if not token:
            raise RuntimeError("Upstox access token is missing. Complete OAuth login first.")
        response = requests.get(
            f"{self._api_base_url}/v3/feed/market-data-feed/authorize",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=self._timeout,
        )
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            # Try token refresh before failing
            if self._refresh_token_callback is not None:
                logger.info("Upstox feed authorize failed; attempting token refresh auth_status=%s", response.status_code)
                refreshed = self._refresh_token_callback()
                if refreshed:
                    logger.info("Token refreshed after feed authorize failure; retrying authorize")
                    token = self._access_token_getter()
                    if token:
                        response = requests.get(
                            f"{self._api_base_url}/v3/feed/market-data-feed/authorize",
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                            timeout=self._timeout,
                        )
                        payload = response.json() if response.content else {}
                        if response.status_code < 400:
                            url = ((payload.get("data") or {}).get("authorized_redirect_uri") if isinstance(payload, dict) else None)
                            if url:
                                logger.info("Upstox feed authorize succeeded after token refresh")
                                return str(url)
            raise RuntimeError(f"Upstox feed authorize failed {response.status_code}: {payload}")
        url = ((payload.get("data") or {}).get("authorized_redirect_uri") if isinstance(payload, dict) else None)
        if not url:
            raise RuntimeError("Upstox feed authorize did not return authorized_redirect_uri")
        logger.info("Upstox V3 feed authorize succeeded")
        return str(url)

    def _run_forever(self) -> None:
        delay = _RECONNECT_BASE_DELAY
        while True:
            with self._lock:
                if not self._running:
                    break
                self._state = "reconnecting"
                self._reconnects += 1
            try:
                if websocket is None:
                    raise RuntimeError("websocket-client is not installed. Run pip install -r backend/requirements.txt.")
                url = self.authorize_url()
                logger.info("Upstox V3 feed connecting attempt=%s", self._reconnects)
                self._ws_app = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_data=self._on_data,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws_app.run_forever(skip_utf8_validation=True, ping_interval=20, ping_timeout=10)
                # If we get here, the connection closed cleanly — reset delay
                with self._lock:
                    self._consecutive_failures = 0
                delay = _RECONNECT_BASE_DELAY
            except Exception as exc:
                with self._lock:
                    self._last_error = str(exc)[:1000]
                    self._connected = False
                    self._state = "reconnecting"
                    self._consecutive_failures += 1
                logger.warning("Upstox V3 feed connection failed attempt=%s error=%s consecutive_failures=%s", self._reconnects, exc, self._consecutive_failures)
                # Give up after too many consecutive failures
                if self._consecutive_failures >= _RECONNECT_MAX_CONSECUTIVE_FAILURES:
                    logger.error("Upstox V3 feed giving up after %s consecutive failures; stopping reconnect loop", self._consecutive_failures)
                    with self._lock:
                        self._running = False
                        self._state = "dead"
                        self._last_error = f"{_RECONNECT_MAX_CONSECUTIVE_FAILURES} consecutive failures, giving up"
                    break
                # Exponential backoff with jitter
                delay = min(delay * 1.5, _RECONNECT_MAX_DELAY)
                jitter = delay * _RECONNECT_JITTER_FACTOR * (2 * random.random() - 1)
                actual_delay = max(0.5, delay + jitter)
                logger.info("Upstox V3 feed reconnecting in %.1fs (attempt %s)", actual_delay, self._reconnects)
                with self._lock:
                    should_run = self._running
                if should_run:
                    time.sleep(actual_delay)
            else:
                with self._lock:
                    should_run = self._running
                if should_run:
                    time.sleep(_RECONNECT_BASE_DELAY)

    def _on_open(self, ws: Any) -> None:
        with self._lock:
            self._connected = True
            self._state = "connected"
            self._last_error = None
            self._consecutive_failures = 0
            modes: Dict[str, list[str]] = {}
            for key, mode in self._subscribed.items():
                modes.setdefault(mode, []).append(key)
            for mode, keys in modes.items():
                self._send_subscription_locked(keys, mode)
        logger.info(
            "Upstox V3 feed connected; reconnect resubscribed=%s across %s modes (attempt %s)",
            sum(len(v) for v in modes.values()), len(modes), self._reconnects
        )

    def _send_subscription_locked(self, keys: Iterable[str], mode: str) -> None:
        if not self._ws_app or not self._ws_app.sock:
            return
        keys_list = list(keys)
        payload = build_subscription_payload(keys_list, mode=mode)
        self._ws_app.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
        logger.info("Upstox V3 subscription sent mode=%s instrument_count=%s", mode, len(keys_list))

    def _handle_binary_frame(self, raw_bytes: bytes) -> None:
        raw_len = len(raw_bytes)
        logger.info("Upstox V3 binary frame received, length=%s", raw_len)
        try:
            decoded = decode_feed_response(raw_bytes)
            feed_count = len(decoded.get("feeds") or {})
            logger.info("Upstox V3 frame decode success, feeds=%s", feed_count)
            self.apply_decoded_message(decoded)
        except Exception as exc:
            with self._lock:
                self._last_error = f"protobuf decode failed: {exc}"[:1000]
            logger.warning("Upstox V3 feed protobuf decode failed: %s", exc)

    def _on_message(self, ws: Any, message: Any) -> None:
        if isinstance(message, str):
            logger.debug("Upstox V3 feed text frame ignored: %s", message[:200])
            return
        self._handle_binary_frame(bytes(message))

    def _on_data(self, ws: Any, data: Any, opcode: Any, fin: Any) -> None:
        is_binary = False
        try:
            import websocket
            if opcode == websocket.ABNF.OPCODE_BINARY:
                is_binary = True
        except Exception:
            if opcode == 2:
                is_binary = True
        if is_binary:
            self._handle_binary_frame(bytes(data))

    def apply_decoded_message(self, decoded: Dict[str, Any]) -> None:
        feeds = decoded.get("feeds") or {}
        current_ts = decoded.get("currentTs") or decoded.get("current_ts")
        updated = 0
        with self._lock:
            for instrument_key, feed in feeds.items():
                tick = extract_ltp_tick(instrument_key, feed, current_ts=current_ts)
                if tick:
                    self._ticks[instrument_key] = tick
                    self._last_tick_time = tick["received_at"]
                    updated += 1
                    logger.info(
                        "Upstox V3 tick accepted key=%s ltp=%s cache_size=%s",
                        instrument_key,
                        tick.get("ltp"),
                        len(self._ticks),
                    )
                    logger.debug(
                        "Upstox V3 quote received key=%s ltp=%s timestamp_source=%s received_at=%s",
                        instrument_key,
                        tick.get("ltp"),
                        tick.get("timestamp_source"),
                        tick.get("received_at"),
                    )
                    if not self._first_tick_logged:
                        self._first_tick_logged = True
                        logger.info("Upstox V3 first tick received key=%s ltp=%s", instrument_key, tick.get("ltp"))
        if updated:
            logger.debug("Upstox V3 feed updated %s ticks", updated)

    def _on_error(self, ws: Any, error: Any) -> None:
        with self._lock:
            self._last_error = str(error)[:1000]
            self._state = "reconnecting" if self._running else "disconnected"
        logger.warning("Upstox V3 feed websocket error: %s", error)

    def _on_close(self, ws: Any, status_code: Any, msg: Any) -> None:
        with self._lock:
            self._connected = False
            self._state = "reconnecting" if self._running else "disconnected"
        logger.info("Upstox V3 feed closed status=%s msg=%s", status_code, msg)
