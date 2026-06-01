"""Unified broker dispatch layer with strict payload validation and normalized errors."""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional, List
import asyncio
import time

from pydantic import BaseModel, Field, field_validator

from brokers import normalization

logger = logging.getLogger("quantg.execution_bridge")


class BrokerOrderRateLimiter:
    def __init__(self, max_per_sec: int = 10, max_per_min: int = 400) -> None:
        self._lock = asyncio.Lock()
        self._history: List[float] = []
        self._max_per_sec = max_per_sec
        self._max_per_min = max_per_min

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._history = [t for t in self._history if now - t < 60.0]
                last_sec = [t for t in self._history if now - t < 1.0]
                if len(last_sec) < self._max_per_sec and len(self._history) < self._max_per_min:
                    self._history.append(now)
                    return
                sleep_time = 0.05
                if len(last_sec) >= self._max_per_sec:
                    sleep_time = max(sleep_time, 1.0 - (now - last_sec[0]))
                if len(self._history) >= self._max_per_min:
                    sleep_time = max(sleep_time, 60.0 - (now - self._history[0]))
                await asyncio.sleep(sleep_time)


_BROKER_LIMITERS = {
    "zerodha": BrokerOrderRateLimiter(10, 400),
    "upstox": BrokerOrderRateLimiter(10, 400),
}


SUPPORTED_SEGMENTS = {"EQUITY", "OPTIONS", "FUTURES", "COMMODITY", "NSE_EQ", "BSE_EQ", "NSE_FO", "BSE_FO", "MCX_FO"}
SUPPORTED_ORDER_TYPES = {"MARKET", "LIMIT"}
SUPPORTED_SIDES = {"BUY", "SELL"}


class ExecutionDispatchPayload(BaseModel):
    broker: str
    segment: str
    exchange: str
    tradingsymbol: str
    instrument_token: str = ""
    quantity: int = Field(gt=0)
    order_type: str = "MARKET"
    side: str
    product: str = "MIS"
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    tag: Optional[str] = None
    validity: str = "DAY"

    @field_validator("segment")
    @classmethod
    def validate_segment(cls, value: str) -> str:
        text = (value or "").strip().upper()
        if text not in SUPPORTED_SEGMENTS:
            raise ValueError(f"segment must be one of {sorted(SUPPORTED_SEGMENTS)}")
        return text

    @field_validator("order_type")
    @classmethod
    def validate_order_type(cls, value: str) -> str:
        text = (value or "MARKET").strip().upper()
        if text not in SUPPORTED_ORDER_TYPES:
            raise ValueError(f"order_type must be one of {sorted(SUPPORTED_ORDER_TYPES)}")
        return text

    @field_validator("side")
    @classmethod
    def validate_side(cls, value: str) -> str:
        text = (value or "").strip().upper()
        if text not in SUPPORTED_SIDES:
            raise ValueError("side must be BUY or SELL")
        return text

    @field_validator("quantity")
    @classmethod
    def validate_quantity(cls, value: int) -> int:
        if int(value) <= 0:
            raise ValueError("quantity must be greater than zero")
        return int(value)


def canonical_broker(value: str) -> str:
    broker = (value or "zerodha").strip().lower()
    if broker in {"kotak_neo", "kotak"}:
        return "kotak"
    return broker


def runtime_broker(value: str) -> str:
    broker = canonical_broker(value)
    if broker == "kotak":
        return "kotak_neo"
    return broker


def segment_from_exchange(exchange: str, asset_class: str = "DIRECT") -> str:
    exch = (exchange or "NSE").upper()
    asset = (asset_class or "DIRECT").upper()
    if asset in {"OPTION_LONG", "OPTION_SHORT"}:
        return "BSE_FO" if exch in {"BFO", "BSE"} else "MCX_FO" if exch == "MCX" else "NSE_FO"
    if exch == "MCX":
        return "MCX_FO"
    if exch in {"NFO", "BFO", "CDS"}:
        return "BSE_FO" if exch == "BFO" else "NSE_FO"
    return "BSE_EQ" if exch == "BSE" else "NSE_EQ"


def default_product(segment: str, exchange: str, requested: Optional[str] = None) -> str:
    if requested:
        return str(requested).upper()
    if segment in {"OPTIONS", "FUTURES", "COMMODITY", "NSE_FO", "BSE_FO", "MCX_FO"} or (exchange or "").upper() in {"NFO", "BFO", "MCX", "CDS"}:
        return "NRML"
    return "MIS"


def payload_from_intent(
    *,
    broker: str,
    segment: str,
    exchange: str,
    tradingsymbol: str,
    instrument_token: str,
    quantity: int,
    intent: str,
    order_type: str,
    product: str,
    price: Optional[float],
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    tag: Optional[str] = None,
) -> ExecutionDispatchPayload:
    side = "SELL" if intent in {"OPEN_SHORT", "CLOSE_LONG"} else "BUY"
    return ExecutionDispatchPayload(
        broker=runtime_broker(broker),
        segment=segment,
        exchange=(exchange or "NSE").upper(),
        tradingsymbol=(tradingsymbol or "").upper(),
        instrument_token=str(instrument_token or ""),
        quantity=int(quantity),
        order_type=order_type,
        side=side,
        product=default_product(segment, exchange, product),
        price=price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        tag=tag,
    )


def normalize_broker_failure(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return "Broker rejected the order without a detailed message."
    if len(text) > 500:
        return text[:497] + "..."
    return text


UpstoxPlaceFn = Callable[..., Awaitable[Dict[str, Any]]]
KitePlaceFn = Callable[..., Awaitable[Dict[str, Any]]]
UpstoxTokenFn = Callable[[str, str, str], Optional[str]]


async def submit_order(
    user_id: str,
    payload: ExecutionDispatchPayload,
    *,
    place_upstox: UpstoxPlaceFn,
    place_kite: KitePlaceFn,
    resolve_upstox_token: UpstoxTokenFn,
) -> Dict[str, Any]:
    """Place order through the configured broker. Never raises — returns ok/error envelope."""
    try:
        validated = ExecutionDispatchPayload.model_validate(payload.model_dump())
    except Exception as exc:
        return {
            "ok": False,
            "status": "FAILED",
            "error": normalize_broker_failure(exc),
            "broker_order_id": None,
            "raw": None,
            "attempts": 0,
            "recovered": False,
        }

    broker = runtime_broker(validated.broker)
    limiter = _BROKER_LIMITERS.get(canonical_broker(broker))
    if limiter:
        await limiter.acquire()

    try:


        if broker == "upstox":
            token = resolve_upstox_token(validated.exchange, validated.tradingsymbol, validated.instrument_token)
            if not token:
                raise RuntimeError(
                    f"Upstox instrument token unavailable for {validated.tradingsymbol} ({validated.segment})."
                )
            result = await place_upstox(
                user_id,
                instrument_token=token,
                side=validated.side,
                quantity=validated.quantity,
                order_type=validated.order_type,
                product=validated.product,
                price=validated.price,
                tag=validated.tag,
            )
            broker_order_id = result.get("broker_order_id") or result.get("order_id")
            if result.get("ok") is False:
                raise RuntimeError(result.get("error") or "Upstox order rejected")
            return {
                "ok": True,
                "status": "PENDING_BROKER",
                "broker_order_id": broker_order_id,
                "raw": result.get("raw") or result,
                "attempts": int(result.get("attempts") or 1),
                "recovered": False,
                "error": None,
                "dispatch": validated.model_dump(),
            }

        result = await place_kite(
            user_id,
            tradingsymbol=validated.tradingsymbol,
            exchange=validated.exchange,
            transaction_type=validated.side,
            quantity=validated.quantity,
            order_type=validated.order_type,
            product=validated.product,
            price=validated.price,
            tag=validated.tag,
        )
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Kite order rejected")
        return {
            "ok": True,
            "status": "PENDING_BROKER",
            "broker_order_id": result.get("order_id"),
            "raw": result.get("broker_order") or result,
            "attempts": int(result.get("attempts") or 1),
            "recovered": bool(result.get("recovered")),
            "error": None,
            "dispatch": validated.model_dump(),
        }
    except Exception as exc:
        message = normalize_broker_failure(exc)
        logger.warning(
            "execution_bridge submit failed user=%s broker=%s symbol=%s: %s",
            user_id,
            broker,
            validated.tradingsymbol,
            message,
        )
        return {
            "ok": False,
            "status": "FAILED",
            "error": message,
            "broker_order_id": None,
            "raw": None,
            "attempts": 0,
            "recovered": False,
            "dispatch": validated.model_dump(),
        }


def normalize_order_row(row: Dict[str, Any]) -> Dict[str, Any]:
    status = normalization.normalize_order_status(row.get("status")) or str(row.get("status") or "OPEN").upper()
    return {
        "id": row.get("id"),
        "broker_order_id": row.get("broker_order_id"),
        "symbol": row.get("symbol") or row.get("tradingsymbol"),
        "exchange": row.get("exchange"),
        "segment": row.get("segment") or segment_from_exchange(row.get("exchange") or "NSE", row.get("asset_class") or "DIRECT"),
        "side": row.get("side"),
        "qty": row.get("qty") or row.get("quantity"),
        "filled_qty": row.get("filled_qty"),
        "pending_qty": row.get("pending_qty"),
        "price": row.get("price"),
        "order_type": row.get("order_type"),
        "product": row.get("product"),
        "status": status,
        "execution_status": status,
        "status_message": row.get("status_message"),
        "mode": row.get("mode"),
        "broker": row.get("broker"),
        "strategy_id": row.get("strategy_id"),
        "source": row.get("source"),
        "created_at": row.get("created_at"),
        "stop_loss": row.get("stop_loss"),
        "take_profit": row.get("take_profit"),
        "instrument_token": (row.get("instrument") or {}).get("instrument_token") or row.get("instrument_token"),
        "reject_reason": row.get("reject_reason"),
        "error_message": row.get("error_message"),
    }
