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
    try:
        exp = datetime.fromisoformat(expires_at_iso.replace("Z", "+00:00"))
    except Exception:
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
) -> Dict[str, Any]:
    """Place a real order via Kite. Raises on failure."""
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
    order_id = kite.place_order(**params)
    return {"order_id": order_id}
