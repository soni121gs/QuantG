"""Upstox Analytics Token client — read-only, 1-year TTL.

Used as a system-level market data source that never requires daily OAuth.
Provides: live LTP, historical candles, option chain.
"""
from __future__ import annotations

import logging
import urllib.parse
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("quantg.upstox_analytics")

UPSTOX_BASE = "https://api.upstox.com"


class UpstoxAnalyticsClient:
    def __init__(self, token: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    async def get_ltp(self, instrument_keys: List[str]) -> Dict[str, Optional[float]]:
        """Returns {instrument_key: ltp} for each key. Missing keys get None."""
        if not instrument_keys:
            return {}
        keys_param = ",".join(instrument_keys)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"{UPSTOX_BASE}/v2/market-quote/ltp",
                    params={"instrument_key": keys_param},
                    headers=self._headers,
                )
            r.raise_for_status()
            data = r.json().get("data", {})
            out: Dict[str, Optional[float]] = {}
            for key in instrument_keys:
                colon_key = key.replace("|", ":")
                node = data.get(key) or data.get(colon_key) or {}
                ltp = node.get("last_price")
                out[key] = float(ltp) if ltp is not None else None
            return out
        except Exception as exc:
            logger.warning("Analytics LTP failed: %s", exc)
            return {k: None for k in instrument_keys}

    async def get_historical_candles(
        self,
        instrument_key: str,
        interval: str = "1day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Returns list of OHLCV dicts sorted oldest-first.

        interval: "1minute" | "30minute" | "1day" | "1week" | "1month"
        from_date / to_date: "YYYY-MM-DD" (defaults to last 30 days)
        """
        today = date.today().isoformat()
        _to = to_date or today
        _from = from_date or (date.today() - timedelta(days=30)).isoformat()
        encoded = urllib.parse.quote(instrument_key, safe="")
        url = f"{UPSTOX_BASE}/v2/historical-candle/{encoded}/{interval}/{_to}/{_from}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(url, headers=self._headers)
            r.raise_for_status()
            raw = r.json().get("data", {}).get("candles", [])
            # Each row: [timestamp, open, high, low, close, volume, oi]
            return [
                {
                    "timestamp": c[0],
                    "open":  c[1],
                    "high":  c[2],
                    "low":   c[3],
                    "close": c[4],
                    "volume": c[5],
                    "oi": c[6] if len(c) > 6 else None,
                }
                for c in raw
            ]
        except Exception as exc:
            logger.warning("Analytics candles failed for %s: %s", instrument_key, exc)
            return []

    async def get_option_chain(
        self,
        instrument_key: str,
        expiry_date: str,
    ) -> Dict[str, Any]:
        """Returns full Upstox option chain response for instrument_key + expiry_date."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{UPSTOX_BASE}/v2/option/chain",
                    params={"instrument_key": instrument_key, "expiry_date": expiry_date},
                    headers=self._headers,
                )
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("Analytics option chain failed for %s %s: %s", instrument_key, expiry_date, exc)
            return {}

    async def get_option_expiry_dates(self, instrument_key: str) -> List[str]:
        """Returns list of available expiry date strings for an underlying."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"{UPSTOX_BASE}/v2/option/contract",
                    params={"instrument_key": instrument_key},
                    headers=self._headers,
                )
            r.raise_for_status()
            data = r.json().get("data", [])
            return sorted({d.get("expiry") for d in data if d.get("expiry")})
        except Exception as exc:
            logger.warning("Analytics expiry dates failed for %s: %s", instrument_key, exc)
            return []
