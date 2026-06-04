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
    assert tick["source"] == "websocket"
    assert tick["received_at"]


def test_tick_without_broker_timestamp_marks_server_received_at_fallback():
    tick = extract_ltp_tick("MCX_FO|566995", {"ltpc": {"ltp": 101.25}})
    assert tick["ltp"] == pytest.approx(101.25)
    assert tick["received_at"]
    assert tick["timestamp_source"] == "server_received_at"
    assert tick["timestamp"] == tick["received_at"]


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
    assert status["subscribed_keys"] == ["NSE_INDEX|Nifty 50"]


def test_gateway_strategy_reads_latest_tick_from_websocket_cache():
    gateway = UpstoxGateway(access_token="token", api_key="key", api_secret="secret", redirect_uri="http://localhost")
    gateway._feed_v3.apply_decoded_message(decode_feed_response(_ltpc_message_bytes("MCX_FO|566995", 101.25)))
    tick = gateway.latest_tick("MCX_FO|566995")
    assert tick["ltp"] == pytest.approx(101.25)


def test_camelcase_and_snakecase_feed_tick_extraction():
    # Test camelCase structure for marketFF and indexFF
    camel_market_feed = {
        "fullFeed": {
            "marketFF": {
                "ltpc": {"ltp": 250.5, "cp": 248.0, "ltt": 1740729552723, "ltq": 100}
            }
        }
    }
    tick1 = extract_ltp_tick("NSE_EQ|INE002A01018", camel_market_feed)
    assert tick1 is not None
    assert tick1["ltp"] == pytest.approx(250.5)

    camel_index_feed = {
        "fullFeed": {
            "indexFF": {
                "ltpc": {"ltp": 24850.4, "cp": 24800.0, "ltt": 1740729552723}
            }
        }
    }
    tick2 = extract_ltp_tick("NSE_INDEX|Nifty 50", camel_index_feed)
    assert tick2 is not None
    assert tick2["ltp"] == pytest.approx(24850.4)

    camel_greeks_feed = {
        "firstLevelWithGreeks": {
            "ltpc": {"ltp": 45.2, "cp": 44.0}
        }
    }
    tick3 = extract_ltp_tick("NSE_FO|54323", camel_greeks_feed)
    assert tick3 is not None
    assert tick3["ltp"] == pytest.approx(45.2)

    # Test snake_case structure for market_ff and index_ff
    snake_market_feed = {
        "full_feed": {
            "market_ff": {
                "ltpc": {"ltp": 250.5, "cp": 248.0, "ltt": 1740729552723, "ltq": 100}
            }
        }
    }
    tick4 = extract_ltp_tick("NSE_EQ|INE002A01018", snake_market_feed)
    assert tick4 is not None
    assert tick4["ltp"] == pytest.approx(250.5)

    snake_index_feed = {
        "full_feed": {
            "index_ff": {
                "ltpc": {"ltp": 24850.4, "cp": 24800.0, "ltt": 1740729552723}
            }
        }
    }
    tick5 = extract_ltp_tick("NSE_INDEX|Nifty 50", snake_index_feed)
    assert tick5 is not None
    assert tick5["ltp"] == pytest.approx(24850.4)

    snake_greeks_feed = {
        "first_level_with_greeks": {
            "ltpc": {"ltp": 45.2, "cp": 44.0}
        }
    }
    tick6 = extract_ltp_tick("NSE_FO|54323", snake_greeks_feed)
    assert tick6 is not None
    assert tick6["ltp"] == pytest.approx(45.2)


def test_apply_decoded_message_populates_tick_cache_and_latest_tick():
    feed = UpstoxMarketDataFeedV3(access_token_getter=lambda: "token", api_base_url="https://api.upstox.com")
    
    # Send a decoded message with both a camelCase and a snake_case feed
    decoded = {
        "currentTs": 1740729566039,
        "feeds": {
            "NSE_EQ|INE002A01018": {
                "full_feed": {
                    "market_ff": {
                        "ltpc": {"ltp": 255.45, "cp": 250.0}
                    }
                }
            },
            "NSE_INDEX|Nifty 50": {
                "fullFeed": {
                    "indexFF": {
                        "ltpc": {"ltp": 24852.1, "cp": 24800.0}
                    }
                }
            }
        }
    }
    
    feed.apply_decoded_message(decoded)
    
    # 1. apply_decoded_message populates tick cache
    assert len(feed.latest_ticks()) == 2
    
    # 2. latest_tick returns websocket tick
    tick1 = feed.latest_tick("NSE_EQ|INE002A01018")
    assert tick1 is not None
    assert tick1["ltp"] == pytest.approx(255.45)
    
    tick2 = feed.latest_tick("NSE_INDEX|Nifty 50")
    assert tick2 is not None
    assert tick2["ltp"] == pytest.approx(24852.1)


def test_websocket_binary_frame_compatibility_on_message_and_on_data():
    feed = UpstoxMarketDataFeedV3(access_token_getter=lambda: "token", api_base_url="https://api.upstox.com")
    
    # 1. Test binary frame path through on_message
    raw_bytes_msg = _ltpc_message_bytes("NSE_FO|45450", 221.5)
    feed._on_message(None, raw_bytes_msg)
    
    tick_msg = feed.latest_tick("NSE_FO|45450")
    assert tick_msg is not None
    assert tick_msg["ltp"] == pytest.approx(221.5)
    
    # 2. Test binary frame path through on_data (opcode 2 / OPCODE_BINARY)
    raw_bytes_data = _ltpc_message_bytes("MCX_FO|566995", 101.25)
    feed._on_data(None, raw_bytes_data, opcode=2, fin=True)
    
    tick_data = feed.latest_tick("MCX_FO|566995")
    assert tick_data is not None
    assert tick_data["ltp"] == pytest.approx(101.25)
    
    # 3. Test that both cache mappings are populated correctly and coexist
    ticks = feed.latest_ticks()
    assert "NSE_FO|45450" in ticks
    assert "MCX_FO|566995" in ticks
    assert len(ticks) == 2


def test_protobuf_nested_descriptor_layout():
    # Verify nesting of FeedsEntry inside FeedResponse
    feed_response_desc = FeedResponse.DESCRIPTOR
    nested_types = feed_response_desc.nested_types
    nested_names = [nt.name for nt in nested_types]
    assert "FeedsEntry" in nested_names

    # Verify feeds field points to the nested FeedsEntry type
    feeds_field = feed_response_desc.fields_by_name["feeds"]
    assert feeds_field.message_type.name == "FeedsEntry"
    assert feeds_field.message_type.containing_type == feed_response_desc


def test_market_info_handshake_frame_silenced(caplog):
    import logging
    feed = UpstoxMarketDataFeedV3(access_token_getter=lambda: "token", api_base_url="https://api.upstox.com")
    
    # Simulate a market_info frame by serializing a FeedResponse with type=2 (market_info)
    message = FeedResponse()
    message.type = 2  # market_info
    message.currentTs = 1740729566039
    message.marketInfo.segmentStatus["NSE"] = 2  # NORMAL_OPEN
    raw_bytes = message.SerializeToString()
    
    with caplog.at_level(logging.INFO):
        feed._on_message(None, raw_bytes)
        
    # Check that it printed handshake successful log
    assert any("handshake successful: segment status received" in record.message for record in caplog.records)
    
    # Assert tick cache was NOT updated (remains empty)
    assert len(feed.latest_ticks()) == 0
