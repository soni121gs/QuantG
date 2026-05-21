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


def safe_positions(kite: KiteConnect) -> Optional[Dict[str, Any]]:
    try:
        return kite.positions()
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
        return kite.order_history(order_id)
    except Exception as e:
        logger.warning(f"order_history failed for {order_id}: {e}")
        return None


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
