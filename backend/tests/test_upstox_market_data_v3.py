from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from brokers.upstox_market_data_v3 import (
    FeedResponse,
    UpstoxMarketDataFeedV3,
    build_subscription_payload,
    decode_feed_response,
    extract_ltp_tick,
)
from brokers.upstox_gateway import UpstoxGateway


def _ltpc_message_bytes(instrument_key: str = "NSE_FO|45450", ltp: float = 219.3) -> bytes:
    message = FeedResponse()
    message.type = 1
    message.currentTs = 1740729566039
    feed = message.feeds[instrument_key]
    feed.ltpc.ltp = ltp
    feed.ltpc.ltt = 1740729552723
    feed.ltpc.ltq = 75
    feed.ltpc.cp = 494.05
    return message.SerializeToString()


def test_subscription_payload_is_binary_json_for_upstox_v3():
    payload = build_subscription_payload(["NSE_FO|45450", "NSE_FO|45450"], mode="full")
    assert isinstance(payload, bytes)
    body = json.loads(payload.decode("utf-8"))
    assert body["method"] == "sub"
    assert body["data"]["mode"] == "full_d5"
    assert body["data"]["instrumentKeys"] == ["NSE_FO|45450"]


def test_protobuf_decoding_and_tick_extraction():
    decoded = decode_feed_response(_ltpc_message_bytes())
    tick = extract_ltp_tick("NSE_FO|45450", decoded["feeds"]["NSE_FO|45450"], current_ts=decoded["currentTs"])
    assert tick["instrument_key"] == "NSE_FO|45450"
    assert tick["ltp"] == pytest.approx(219.3)
    assert tick["source"] == "upstox-market-data-feed-v3"


def test_feed_client_initialization_and_tick_cache_update():
    feed = UpstoxMarketDataFeedV3(access_token_getter=lambda: "token", api_base_url="https://api.upstox.com")
    feed.apply_decoded_message(decode_feed_response(_ltpc_message_bytes(ltp=221.5)))
    tick = feed.latest_tick("NSE_FO|45450")
    assert tick["ltp"] == pytest.approx(221.5)
    status = feed.status()
    assert status["feed"] == "upstox-market-data-feed-v3"
    assert status["subscribed_count"] == 0


def test_reconnect_loop_sets_reconnecting_and_preserves_subscription(monkeypatch):
    feed = UpstoxMarketDataFeedV3(access_token_getter=lambda: "token", api_base_url="https://api.upstox.com")
    monkeypatch.setattr(feed, "_run_forever", lambda: None)
    result = feed.subscribe(["NSE_INDEX|Nifty 50"], mode="ltpc")
    status = feed.status()
    assert result["ok"] is True
    assert status["state"] == "reconnecting"
    assert status["subscribed_count"] == 1


def test_gateway_strategy_reads_latest_tick_from_websocket_cache():
    gateway = UpstoxGateway(access_token="token", api_key="key", api_secret="secret", redirect_uri="http://localhost")
    gateway._feed_v3.apply_decoded_message(decode_feed_response(_ltpc_message_bytes("MCX_FO|566995", 101.25)))
    tick = gateway.latest_tick("MCX_FO|566995")
    assert tick["ltp"] == pytest.approx(101.25)
