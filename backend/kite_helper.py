"""Zerodha Kite Connect helper.

Centralises:
- Per-user KiteConnect instance creation
- Access-token storage / expiry checks
- Symbol -> instrument_token cache
- Safe wrappers that fall back gracefully when no live session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException, KiteException

logger = logging.getLogger("quantg.kite")

# Indian time-zone offset (+05:30). Access tokens issued by Kite expire at 06:00 IST next day.
IST_OFFSET = timedelta(hours=5, minutes=30)


def _now_ist() -> datetime:
    return datetime.now(timezone.utc) + IST_OFFSET


def next_token_expiry_iso() -> str:
    """Kite access tokens are valid until 6 AM IST the next morning."""
    now_ist = _now_ist()
    target = now_ist.replace(hour=6, minute=0, second=0, microsecond=0)
    if now_ist >= target:
        target = target + timedelta(days=1)
    # Convert IST back to UTC for storage
    return (target - IST_OFFSET).replace(tzinfo=timezone.utc).isoformat()


def is_token_valid(expires_at_iso: Optional[str]) -> bool:
    if not expires_at_iso:
        return False
    exp: Optional[datetime] = None
    try:
        exp = datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00"))
    except Exception:
        return False
    if exp is None:
        return False
    return datetime.now(timezone.utc) < exp


def make_kite(api_key: str, access_token: Optional[str] = None) -> KiteConnect:
    kite = KiteConnect(api_key=api_key)
    if access_token:
        kite.set_access_token(access_token)
    return kite


def login_url(api_key: str) -> str:
    return make_kite(api_key).login_url()


def exchange_request_token(api_key: str, api_secret: str, request_token: str) -> Dict[str, Any]:
    """Exchange a request_token for an access_token using api_secret."""
    kite = make_kite(api_key)
    session = kite.generate_session(request_token=request_token, api_secret=api_secret)
    return session  # contains access_token, public_token, user_id, etc.


def safe_ohlc(kite: KiteConnect, instruments: List[str]) -> Optional[Dict[str, Any]]:
    """kite.ohlc returns {last_price, ohlc:{open,high,low,close}} — use for watchlist."""
    try:
        return kite.ohlc(instruments)
    except Exception as e:
        logger.warning(f"Kite ohlc error: {e}")
        return None


def safe_ltp(kite: KiteConnect, instruments: List[str]) -> Optional[Dict[str, Any]]:
    """kite.ltp wrapper. Returns None on error so caller can fall back."""
    try:
        return kite.ltp(instruments)
    except (TokenException, KiteException) as e:
        logger.warning(f"Kite ltp error: {e}")
        return None
    except Exception as e:
        logger.warning(f"Unexpected ltp error: {e}")
        return None


def safe_quote(kite: KiteConnect, instruments: List[str]) -> Optional[Dict[str, Any]]:
    try:
        return kite.quote(instruments)
    except Exception as e:
        logger.warning(f"Kite quote error: {e}")
        return None


def _first_value(row: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def normalize_order_status(status: Any) -> Optional[str]:
    if status in (None, ""):
        return None
    text = str(status).strip().upper()
    mapping = {
        "COMPLETE": "COMPLETE",
        "COMPLETED": "COMPLETE",
        "TRADED": "COMPLETE",
        "CANCELLED": "CANCELLED",
        "CANCELED": "CANCELLED",
        "REJECTED": "REJECTED",
        "OPEN": "OPEN",
        "PENDING": "PENDING",
        "TRIGGER PENDING": "TRIGGER PENDING",
        "MODIFY PENDING": "MODIFY PENDING",
        "VALIDATION PENDING": "VALIDATION PENDING",
        "PUT ORDER REQ RECEIVED": "PUT ORDER REQ RECEIVED",
    }
    return mapping.get(text, text)


def normalize_position_row(
    raw: Dict[str, Any],
    *,
    broker: str = "zerodha",
    tradingsymbol: Optional[str] = None,
    quantity: Optional[int] = None,
    average_price: Optional[float] = None,
    last_price: Optional[float] = None,
    pnl: Optional[float] = None,
    product: Optional[str] = None,
    exchange: Optional[str] = None,
    instrument_token: Optional[str] = None,
) -> Dict[str, Any]:
    symbol = str(tradingsymbol or _first_value(raw, ["tradingsymbol", "trading_symbol", "tradingSymbol", "symbol", "trdSym"]) or "").upper()
    try:
        qty = int(quantity if quantity is not None else float(_first_value(raw, ["quantity", "netQty", "net_quantity", "qty", "net_quantity"]) or 0))
    except Exception:
        qty = 0
    avg = float(average_price if average_price is not None else _first_value(raw, ["average_price", "avg_price", "averagePrice", "avgPrc"]) or 0)
    ltp = float(last_price if last_price is not None else _first_value(raw, ["last_price", "ltp", "lastTradedPrice"]) or avg or 0)
    exch = str(exchange or _first_value(raw, ["exchange", "exSeg", "exchange_segment"]) or ("NFO" if symbol.endswith(("CE", "PE")) else "NSE")).upper()
    computed_pnl = float(pnl if pnl is not None else _first_value(raw, ["pnl", "unrealisedPnl", "unrealized_pnl", "unrealised_pnl"]) or ((ltp - avg) * qty))
    row = {
        "broker": broker,
        "tradingsymbol": symbol,
        "quantity": qty,
        "average_price": round(avg, 2),
        "last_price": round(ltp, 2),
        "pnl": round(computed_pnl, 2),
        "product": _first_value(raw, ["product", "prod"]) or product or "",
        "exchange": exch,
        "instrument_token": str(instrument_token or _first_value(raw, ["instrument_token", "instrument_key", "instrumentToken"]) or ""),
        # Broker-specific aliases kept for legacy callers.
        "trdSym": symbol,
        "netQty": qty,
        "avgPrc": round(avg, 2),
        "ltp": round(ltp, 2),
    }
    return row


def normalize_positions_payload(raw: Any, *, broker: str = "zerodha") -> Dict[str, List[Dict[str, Any]]]:
    if isinstance(raw, dict) and isinstance(raw.get("net"), list):
        net_rows = raw.get("net") or []
        day_rows = raw.get("day") or []
        return {
            "net": [normalize_position_row(row, broker=broker) for row in net_rows if isinstance(row, dict)],
            "day": [normalize_position_row(row, broker=broker) for row in day_rows if isinstance(row, dict)],
        }
    rows: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        rows = [row for row in raw if isinstance(row, dict)]
    elif isinstance(raw, dict):
        for key in ("data", "positions", "positionBook", "position_book", "result", "records", "net"):
            value = raw.get(key)
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
        if not rows:
            for value in raw.values():
                if isinstance(value, list):
                    rows.extend(item for item in value if isinstance(item, dict))
    net = []
    for row in rows:
        normalized = normalize_position_row(row, broker=broker)
        if normalized["quantity"] != 0:
            net.append(normalized)
    return {"net": net, "day": []}


def normalize_order_update(raw: Dict[str, Any], *, broker: str = "zerodha") -> Dict[str, Any]:
    order_id = str(_first_value(raw, ["order_id", "orderId", "nOrdNo", "orderNo"]) or "")
    symbol = str(_first_value(raw, ["tradingsymbol", "trading_symbol", "tradingSymbol", "trdSym", "symbol"]) or "").upper()
    status = normalize_order_status(_first_value(raw, ["status", "ordSt", "order_status", "orderStatus", "ordStatus"]))
    try:
        qty = int(float(_first_value(raw, ["quantity", "qty", "orderQty"]) or 0))
    except Exception:
        qty = 0
    try:
        filled_qty = int(float(_first_value(raw, ["filled_quantity", "filledQty", "fldQty", "filled_quantity"]) or 0))
    except Exception:
        filled_qty = 0
    try:
        pending_qty = int(float(_first_value(raw, ["pending_quantity", "pendingQty", "unfilledQty", "remainingQty"]) or max(0, qty - filled_qty)))
    except Exception:
        pending_qty = max(0, qty - filled_qty)
    avg_price = float(_first_value(raw, ["average_price", "avg_price", "averagePrice", "avgPrc", "price"]) or 0)
    return {
        "broker": broker,
        "order_id": order_id,
        "tradingsymbol": symbol,
        "transaction_type": str(_first_value(raw, ["transaction_type", "transactionType", "trnsTp", "side"]) or "").upper(),
        "quantity": qty,
        "filled_quantity": filled_qty,
        "filled_qty": filled_qty,
        "pending_quantity": pending_qty,
        "pending_qty": pending_qty,
        "average_price": round(avg_price, 2),
        "price": round(float(_first_value(raw, ["price"]) or avg_price or 0), 2),
        "order_type": str(_first_value(raw, ["order_type", "orderType", "ordTyp"]) or ""),
        "product": str(_first_value(raw, ["product", "prod"]) or ""),
        "status": status,
        "status_message": str(_first_value(raw, ["status_message", "statusMessage", "rejRsn", "reason"]) or status or ""),
        "order_timestamp": _first_value(raw, ["order_timestamp", "orderTimestamp", "order_time", "exchOrdTm"]),
        "exchange": str(_first_value(raw, ["exchange", "exSeg", "exchange_segment"]) or "").upper(),
        "instrument_token": str(_first_value(raw, ["instrument_token", "instrument_key", "instrumentToken"]) or ""),
        "raw": raw,
    }


def safe_positions(kite: KiteConnect) -> Optional[Dict[str, Any]]:
    try:
        return normalize_positions_payload(kite.positions(), broker="zerodha")
    except Exception as e:
        logger.warning(f"Kite positions error: {e}")
        return None


def safe_holdings(kite: KiteConnect) -> Optional[List[Dict[str, Any]]]:
    try:
        return kite.holdings()
    except Exception as e:
        logger.warning(f"Kite holdings error: {e}")
        return None


# ===== Instrument-token cache (refreshed every 12h) =====
_INSTRUMENT_CACHE: Dict[str, Any] = {"by_segment": {}}


def instrument_token(kite: KiteConnect, symbol: str, segment: str = "NSE") -> Optional[int]:
    """Resolve a tradingsymbol to its instrument_token for one exchange segment.

    The cache is keyed by segment. This matters for index symbols such as
    BSE:SENSEX; an NSE cache must not be reused for BSE lookups.
    """
    sym = symbol.upper()
    segment = (segment or "NSE").upper()
    now = datetime.now(timezone.utc)
    segment_cache = _INSTRUMENT_CACHE["by_segment"].get(segment) or {"by_symbol": {}, "cached_at": None}
    cached_at = segment_cache["cached_at"]
    stale = cached_at is None or (now - cached_at).total_seconds() > 43200
    if stale:
        try:
            instruments = kite.instruments(segment)
            by_symbol = {
                i["tradingsymbol"].upper(): int(i["instrument_token"])
                for i in instruments
                if i.get("tradingsymbol") and i.get("instrument_token")
            }
            segment_cache = {"by_symbol": by_symbol, "cached_at": now}
            _INSTRUMENT_CACHE["by_segment"][segment] = segment_cache
            logger.info(f"instrument cache refreshed: segment={segment} symbols={len(by_symbol)}")
        except Exception as e:
            logger.warning(f"instruments load failed for segment {segment}: {e}")
            if not segment_cache["by_symbol"]:
                return None
    return segment_cache["by_symbol"].get(sym)


def safe_historical(
    kite: KiteConnect,
    instrument_tok: int,
    days: int = 60,
    interval: str = "day",
) -> Optional[List[Dict[str, Any]]]:
    """Fetch OHLC candles from Kite and return [{date, close, open, high, low, volume}].
    interval: minute, 3minute, 5minute, 15minute, 30minute, 60minute, day."""
    try:
        from_date = datetime.now() - timedelta(days=days)
        to_date = datetime.now()
        candles = kite.historical_data(instrument_tok, from_date, to_date, interval)
        out = []
        for c in candles:
            d = c.get("date")
            if hasattr(d, "strftime"):
                date_str = d.strftime("%Y-%m-%d") if interval == "day" else d.strftime("%Y-%m-%d %H:%M")
            else:
                date_str = str(d)
            out.append({
                "date": date_str,
                "close": float(c.get("close", 0)),
                "open": float(c.get("open", 0)),
                "high": float(c.get("high", 0)),
                "low": float(c.get("low", 0)),
                "volume": int(c.get("volume", 0)),
            })
        return out
    except Exception as e:
        logger.warning(f"historical_data failed for token {instrument_tok}: {e}")
        return None


def safe_order_history(kite: KiteConnect, order_id: str) -> Optional[List[Dict[str, Any]]]:
    """Fetch full order_history for a single broker order."""
    try:
        history = kite.order_history(order_id) or []
        return [normalize_order_update(row, broker="zerodha") for row in history if isinstance(row, dict)]
    except Exception as e:
        logger.warning(f"order_history failed for {order_id}: {e}")
        return None


def normalize_order_book(orders: Optional[List[Dict[str, Any]]], *, broker: str = "zerodha") -> List[Dict[str, Any]]:
    return [normalize_order_update(row, broker=broker) for row in (orders or []) if isinstance(row, dict)]


def place_live_order(
    kite: KiteConnect,
    *,
    tradingsymbol: str,
    exchange: str,
    transaction_type: str,
    quantity: int,
    order_type: str,
    product: str,
    price: Optional[float] = None,
    variety: str = "regular",
    market_protection: float = 5.0,
    tag: Optional[str] = None,
) -> Dict[str, Any]:
    """Place a real order via Kite. Raises on failure.

    market_protection: percent slippage tolerance for MARKET orders (Zerodha
    rejects market orders without this). Default 5% — order auto-cancels if the
    fill price deviates more than 5% from LTP. Ignored for LIMIT orders.
    """
    params = dict(
        variety=variety,
        exchange=exchange,
        tradingsymbol=tradingsymbol,
        transaction_type=transaction_type,
        quantity=int(quantity),
        order_type=order_type,
        product=product,
    )
    if order_type == "LIMIT" and price:
        params["price"] = float(price)
    if order_type == "MARKET":
        # Zerodha requires this for ALL market orders placed via API.
        params["market_protection"] = float(market_protection)
    if tag:
        params["tag"] = str(tag)[:20]
    order_id = kite.place_order(**params)
    return {"order_id": order_id, "tag": params.get("tag")}
