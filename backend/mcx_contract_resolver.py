"""Upstox MCX option contract resolver.

The strategy runner needs a deterministic path for commodity options. Upstox's
search API is useful as a fallback, but live strategy execution should resolve
against the daily instrument master so expired or guessed symbols never reach
order placement.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import time
import urllib.request
from datetime import date, datetime, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional

from pymongo import ReplaceOne

logger = logging.getLogger("quantg.mcx_resolver")

UPSTOX_MCX_MASTER_URL = os.environ.get(
    "UPSTOX_MCX_MASTER_URL",
    "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz",
)
UPSTOX_MCX_CACHE_TTL_SEC = int(os.environ.get("UPSTOX_MCX_CACHE_TTL_SEC", str(24 * 3600)))
UPSTOX_MCX_REFRESH_MIN_INTERVAL_SEC = int(os.environ.get("UPSTOX_MCX_REFRESH_MIN_INTERVAL_SEC", "900"))
MCX_MASTER_CACHE_SCHEMA_VERSION = 2

IST_OFFSET = timedelta(hours=5, minutes=30)
MCX_OPEN_MINUTE = int(os.environ.get("MCX_OPEN_MINUTE", str(9 * 60)))
MCX_CLOSE_MINUTE = int(os.environ.get("MCX_CLOSE_MINUTE", str(23 * 60 + 30)))

SUPPORTED_MCX_UNDERLYINGS = {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ist_now() -> datetime:
    return _utc_now() + IST_OFFSET


def _is_mcx_market_open(now_utc: Optional[datetime] = None) -> bool:
    now_utc = now_utc or _utc_now()
    ist_now = now_utc + IST_OFFSET
    minutes = ist_now.hour * 60 + ist_now.minute
    return ist_now.weekday() < 5 and MCX_OPEN_MINUTE <= minutes <= MCX_CLOSE_MINUTE


def _parse_expiry(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        # Upstox JSON commonly stores expiry as epoch milliseconds.
        try:
            val = float(value)
            if val > 10_000_000_000:
                return datetime.fromtimestamp(val / 1000, tz=timezone.utc).date()
            return datetime.fromtimestamp(val, tz=timezone.utc).date()
        except Exception:
            return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d %b %y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except Exception:
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except Exception:
        return None


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def _master_instrument_key(row: Dict[str, Any]) -> str:
    key = str(row.get("instrument_key") or "").strip()
    if "|" in key:
        return key
    token = str(row.get("instrument_token") or "").strip()
    if "|" in token:
        return token
    return ""


def _row_option_type(row: Dict[str, Any]) -> str:
    opt = str(row.get("option_type") or "").upper().strip()
    inst = str(row.get("instrument_type") or "").upper().strip()
    if opt in {"CE", "PE"}:
        return opt
    if inst in {"CE", "PE"}:
        return inst
    return ""


def _is_option_row(row: Dict[str, Any]) -> bool:
    segment = str(row.get("segment") or row.get("exchange_segment") or "").upper()
    exchange = str(row.get("exchange") or "").upper()
    inst = str(row.get("instrument_type") or "").upper()
    return (
        exchange == "MCX"
        and (segment in {"", "MCX_FO", "MCX"} or segment.startswith("MCX"))
        and (inst in {"CE", "PE", "OPTCOM"} or str(row.get("option_type") or "").upper() in {"CE", "PE"})
    )


def _is_future_row(row: Dict[str, Any]) -> bool:
    segment = str(row.get("segment") or row.get("exchange_segment") or "").upper()
    exchange = str(row.get("exchange") or "").upper()
    inst = str(row.get("instrument_type") or "").upper()
    return (
        exchange == "MCX"
        and (segment in {"", "MCX_FO", "MCX"} or segment.startswith("MCX"))
        and inst in {"FUT", "FUTCOM", "FUTURES"}
    )


def _normalize_underlying(value: Any) -> str:
    text = str(value or "").upper().replace(" ", "").replace("_", "")
    aliases = {
        "NATGAS": "NATURALGAS",
        "NATURALGASMINI": "NATGASMINI",
        "CRUDEOILMINI": "CRUDEOILM",
    }
    return aliases.get(text, text)


def _row_underlying(row: Dict[str, Any]) -> str:
    symbol = str(row.get("trading_symbol") or row.get("tradingsymbol") or "").upper().replace(" ", "")
    # Check trading symbol prefix first for mini contracts like CRUDEOILM
    for underlying in sorted(SUPPORTED_MCX_UNDERLYINGS, key=len, reverse=True):
        if symbol.startswith(underlying):
            return underlying
            
    # Fallback to underlying fields
    for field in ("underlying_symbol", "underlying", "asset_symbol", "root_symbol", "name"):
        value = _normalize_underlying(row.get(field))
        if value in SUPPORTED_MCX_UNDERLYINGS:
            return value
    return ""


def _normalize_contract(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not _is_option_row(row):
        return None
    underlying = _row_underlying(row)
    opt_type = _row_option_type(row)
    expiry = _parse_expiry(row.get("expiry") or row.get("expiry_date"))
    strike = _as_float(row.get("strike_price") or row.get("strike"))
    instrument_token = _master_instrument_key(row)
    trading_symbol = row.get("trading_symbol") or row.get("tradingsymbol")
    if not all([underlying, opt_type, expiry, strike is not None, instrument_token, trading_symbol]):
        logger.debug(
            "dropping incomplete MCX option row underlying=%s opt=%s expiry=%s strike=%s token=%s symbol=%s",
            underlying, opt_type, expiry, strike, bool(instrument_token), trading_symbol,
        )
        return None
    return {
        "cache_key": f"{underlying}:{expiry.isoformat()}:{int(strike) if strike.is_integer() else strike}:{opt_type}",
        "underlying": underlying,
        "exchange": "MCX",
        "segment": "MCX_FO",
        "instrument_type": "OPTCOM",
        "option_type": opt_type,
        "expiry": expiry.isoformat(),
        "strike": float(strike),
        "trading_symbol": str(trading_symbol),
        "tradingsymbol": str(trading_symbol),
        "instrument_token": str(instrument_token),
        "instrument_key": str(instrument_token),
        "lot_size": _as_int(row.get("lot_size") or row.get("minimum_lot"), 1),
        "tick_size": _as_float(row.get("tick_size")) or 0.05,
        "raw_instrument_type": row.get("instrument_type"),
        "updated_at": _utc_now().isoformat(),
    }


def _normalize_future(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not _is_future_row(row):
        return None
    underlying = _row_underlying(row)
    expiry = _parse_expiry(row.get("expiry") or row.get("expiry_date"))
    instrument_token = _master_instrument_key(row)
    trading_symbol = row.get("trading_symbol") or row.get("tradingsymbol")
    if not all([underlying, expiry, instrument_token, trading_symbol]):
        logger.debug(
            "dropping incomplete MCX future row underlying=%s expiry=%s token=%s symbol=%s",
            underlying, expiry, bool(instrument_token), trading_symbol,
        )
        return None
    return {
        "cache_key": f"{underlying}:{expiry.isoformat()}:{trading_symbol}",
        "underlying": underlying,
        "exchange": "MCX",
        "segment": "MCX_FO",
        "instrument_type": "FUTCOM",
        "expiry": expiry.isoformat(),
        "trading_symbol": str(trading_symbol),
        "tradingsymbol": str(trading_symbol),
        "instrument_token": str(instrument_token),
        "instrument_key": str(instrument_token),
        "exchange_token": str(row.get("exchange_token") or ""),
        "lot_size": _as_int(row.get("lot_size") or row.get("minimum_lot"), 1),
        "tick_size": _as_float(row.get("tick_size")) or 0.05,
        "raw_instrument_type": row.get("instrument_type"),
        "updated_at": _utc_now().isoformat(),
    }


class MCXContractResolver:
    def __init__(self, db, master_url: str = UPSTOX_MCX_MASTER_URL):
        self.db = db
        self.master_url = master_url
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_attempt = 0.0

    async def ensure_cache(self, *, force: bool = False, reason: str = "ensure") -> Dict[str, Any]:
        meta = await self.db.upstox_instrument_cache_meta.find_one({"_id": "MCX_MASTER"}, {"_id": 0})
        stale = self._is_stale(meta)
        if not force and not stale:
            return {"ok": True, "refreshed": False, "stale": False, "meta": meta}
        if not force and time.monotonic() - self._last_refresh_attempt < UPSTOX_MCX_REFRESH_MIN_INTERVAL_SEC:
            logger.warning("MCX instrument cache stale but refresh throttled reason=%s meta=%s", reason, meta)
            return {"ok": False, "refreshed": False, "stale": True, "reason": "refresh-throttled", "meta": meta}
        return await self.refresh(reason=reason, force=force)

    def _is_stale(self, meta: Optional[Dict[str, Any]]) -> bool:
        if not meta or not meta.get("refreshed_at"):
            return True
        if int(meta.get("schema_version") or 0) != MCX_MASTER_CACHE_SCHEMA_VERSION:
            return True
        try:
            refreshed = datetime.fromisoformat(str(meta["refreshed_at"]).replace("Z", "+00:00"))
            if refreshed.tzinfo is None:
                refreshed = refreshed.replace(tzinfo=timezone.utc)
            return (_utc_now() - refreshed).total_seconds() > UPSTOX_MCX_CACHE_TTL_SEC
        except Exception:
            return True

    async def refresh(self, *, reason: str = "manual", force: bool = False) -> Dict[str, Any]:
        async with self._refresh_lock:
            if not force:
                meta = await self.db.upstox_instrument_cache_meta.find_one({"_id": "MCX_MASTER"}, {"_id": 0})
                if not self._is_stale(meta):
                    return {"ok": True, "refreshed": False, "stale": False, "meta": meta}
            self._last_refresh_attempt = time.monotonic()
            started = _utc_now()
            logger.info("Refreshing Upstox MCX instrument master reason=%s url=%s", reason, self.master_url)
            try:
                rows = await asyncio.to_thread(self._download_master)
                contracts = [c for c in (_normalize_contract(row) for row in rows) if c]
                futures = [c for c in (_normalize_future(row) for row in rows) if c]
                if not contracts and not futures:
                    raise RuntimeError("Upstox MCX master contained no MCX futures/options")
                await self._replace_cache(contracts, futures, started, reason)
                logger.info("MCX master cache refreshed options=%s futures=%s reason=%s", len(contracts), len(futures), reason)
                return {"ok": True, "refreshed": True, "contracts": len(contracts), "futures": len(futures)}
            except Exception as exc:
                logger.warning("MCX master cache refresh failed reason=%s error=%s", reason, exc)
                await self.db.upstox_instrument_cache_meta.update_one(
                    {"_id": "MCX_MASTER"},
                    {"$set": {
                        "last_error": str(exc)[:500],
                        "last_error_at": _utc_now().isoformat(),
                        "source_url": self.master_url,
                    }},
                    upsert=True,
                )
                return {"ok": False, "refreshed": False, "error": str(exc)}

    def _download_master(self) -> List[Dict[str, Any]]:
        req = urllib.request.Request(self.master_url, headers={"User-Agent": "QuantG/MCXResolver"})
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read()
        if self.master_url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "instruments", "items"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        raise RuntimeError("unexpected Upstox instrument master format")

    async def _replace_cache(self, contracts: List[Dict[str, Any]], futures: List[Dict[str, Any]], started: datetime, reason: str) -> None:
        await self.db.upstox_mcx_option_contracts.delete_many({})
        await self.db.upstox_mcx_future_contracts.delete_many({})
        option_ops = [
            ReplaceOne({"cache_key": c["cache_key"]}, c, upsert=True)
            for c in contracts
        ]
        future_ops = [
            ReplaceOne({"cache_key": c["cache_key"]}, c, upsert=True)
            for c in futures
        ]
        for i in range(0, len(option_ops), 1000):
            await self.db.upstox_mcx_option_contracts.bulk_write(option_ops[i:i + 1000], ordered=False)
        for i in range(0, len(future_ops), 1000):
            await self.db.upstox_mcx_future_contracts.bulk_write(future_ops[i:i + 1000], ordered=False)
        by_underlying: Dict[str, int] = {}
        for contract in contracts:
            by_underlying[contract["underlying"]] = by_underlying.get(contract["underlying"], 0) + 1
        futures_by_underlying: Dict[str, int] = {}
        for future in futures:
            futures_by_underlying[future["underlying"]] = futures_by_underlying.get(future["underlying"], 0) + 1
        await self.db.upstox_instrument_cache_meta.update_one(
            {"_id": "MCX_MASTER"},
            {"$set": {
                "refreshed_at": _utc_now().isoformat(),
                "started_at": started.isoformat(),
                "source_url": self.master_url,
                "contract_count": len(contracts),
                "future_count": len(futures),
                "schema_version": MCX_MASTER_CACHE_SCHEMA_VERSION,
                "by_underlying": by_underlying,
                "futures_by_underlying": futures_by_underlying,
                "reason": reason,
                "market_open": _is_mcx_market_open(),
            }, "$unset": {"last_error": "", "last_error_at": ""}},
            upsert=True,
        )

    async def validate_instrument_key(self, instrument_key: Optional[str]) -> Optional[Dict[str, Any]]:
        key = str(instrument_key or "").strip()
        if not key:
            return None
        row = await self.db.upstox_mcx_future_contracts.find_one({"instrument_key": key}, {"_id": 0})
        if row:
            return row
        return await self.db.upstox_mcx_option_contracts.find_one({"instrument_key": key}, {"_id": 0})

    async def resolve_future(
        self,
        *,
        underlying: str,
        expiry_offset: int = 0,
        allow_refresh: bool = True,
    ) -> Optional[Dict[str, Any]]:
        underlying = _normalize_underlying(underlying)
        if underlying not in SUPPORTED_MCX_UNDERLYINGS:
            logger.warning("MCX future resolution failed: unsupported underlying=%s", underlying)
            return None
        if allow_refresh:
            status = await self.ensure_cache(reason=f"resolve-future:{underlying}")
            if status.get("stale"):
                logger.warning("MCX future resolution using stale cache underlying=%s status=%s", underlying, status)
        today = _ist_now().date().isoformat()
        rows = await self.db.upstox_mcx_future_contracts.find(
            {
                "underlying": underlying,
                "exchange": "MCX",
                "instrument_type": "FUTCOM",
                "expiry": {"$gte": today},
            },
            {"_id": 0},
        ).sort([("expiry", 1), ("trading_symbol", 1)]).to_list(length=200)
        if not rows:
            meta = await self.db.upstox_instrument_cache_meta.find_one({"_id": "MCX_MASTER"}, {"_id": 0})
            logger.warning("MCX future resolution failed: no futures underlying=%s today=%s meta=%s", underlying, today, meta)
            if allow_refresh:
                refreshed = await self.refresh(reason=f"fallback-no-future:{underlying}", force=True)
                if refreshed.get("ok"):
                    return await self.resolve_future(
                        underlying=underlying,
                        expiry_offset=expiry_offset,
                        allow_refresh=False,
                    )
            return None
        expiries = sorted({row["expiry"] for row in rows})
        if expiry_offset >= len(expiries):
            logger.warning(
                "MCX future resolution failed: expiry mismatch underlying=%s requested_offset=%s available=%s",
                underlying, expiry_offset, expiries[:8],
            )
            return None
        expiry = expiries[max(0, int(expiry_offset or 0))]
        selected = next((row for row in rows if row["expiry"] == expiry), None)
        if not selected:
            logger.warning("MCX future resolution failed: no row for chosen expiry underlying=%s expiry=%s", underlying, expiry)
            return None
        logger.info(
            "Resolved MCX future via master underlying=%s expiry=%s key=%s symbol=%s exchange_token=%s",
            underlying,
            selected.get("expiry"),
            selected.get("instrument_key"),
            selected.get("trading_symbol"),
            selected.get("exchange_token"),
        )
        return selected

    async def resolve(
        self,
        *,
        underlying: str,
        spot: float,
        option_type: str,
        strike_interval: int,
        otm_points: int = 0,
        expiry_offset: int = 0,
        allow_refresh: bool = True,
    ) -> Optional[Dict[str, Any]]:
        underlying = _normalize_underlying(underlying)
        option_type = str(option_type or "").upper()
        if underlying not in SUPPORTED_MCX_UNDERLYINGS:
            logger.warning("MCX option resolution failed: unsupported underlying=%s", underlying)
            return None
        if option_type not in {"CE", "PE"}:
            logger.warning("MCX option resolution failed: invalid option_type=%s underlying=%s", option_type, underlying)
            return None
        if not spot or float(spot) <= 0:
            logger.warning("MCX option resolution failed: invalid spot=%s underlying=%s", spot, underlying)
            return None

        if allow_refresh:
            status = await self.ensure_cache(reason=f"resolve:{underlying}")
            if status.get("stale"):
                logger.warning("MCX option resolution using stale cache underlying=%s status=%s", underlying, status)

        target_strike = self._target_strike(float(spot), int(strike_interval or 1), option_type, int(otm_points or 0))
        today = _ist_now().date().isoformat()
        rows = await self.db.upstox_mcx_option_contracts.find(
            {
                "underlying": underlying,
                "exchange": "MCX",
                "instrument_type": "OPTCOM",
                "option_type": option_type,
                "expiry": {"$gte": today},
            },
            {"_id": 0},
        ).sort([("expiry", 1), ("strike", 1)]).to_list(length=5000)

        if not rows:
            meta = await self.db.upstox_instrument_cache_meta.find_one({"_id": "MCX_MASTER"}, {"_id": 0})
            logger.warning(
                "MCX option resolution failed: no contracts for underlying=%s opt=%s today=%s meta=%s",
                underlying, option_type, today, meta,
            )
            if allow_refresh:
                refreshed = await self.refresh(reason=f"fallback-no-contracts:{underlying}", force=True)
                if refreshed.get("ok"):
                    return await self.resolve(
                        underlying=underlying,
                        spot=spot,
                        option_type=option_type,
                        strike_interval=strike_interval,
                        otm_points=otm_points,
                        expiry_offset=expiry_offset,
                        allow_refresh=False,
                    )
            return None

        expiries = sorted({row["expiry"] for row in rows})
        if expiry_offset >= len(expiries):
            logger.warning(
                "MCX option resolution failed: expiry mismatch underlying=%s opt=%s requested_offset=%s available=%s",
                underlying, option_type, expiry_offset, expiries[:8],
            )
            return None
        expiry = expiries[max(0, int(expiry_offset or 0))]
        expiry_rows = [row for row in rows if row["expiry"] == expiry]
        if not expiry_rows:
            logger.warning("MCX option resolution failed: no rows for chosen expiry underlying=%s expiry=%s", underlying, expiry)
            return None

        exact = [row for row in expiry_rows if abs(float(row["strike"]) - target_strike) < 0.0001]
        selected = exact[0] if exact else min(expiry_rows, key=lambda row: abs(float(row["strike"]) - target_strike))
        if not exact:
            available = sorted({float(row["strike"]) for row in expiry_rows})
            nearest_distance = abs(float(selected["strike"]) - target_strike)
            if nearest_distance > max(1, int(strike_interval or 1)):
                logger.warning(
                    "MCX option resolution failed: no strike found underlying=%s opt=%s expiry=%s target=%s nearest=%s available_sample=%s",
                    underlying, option_type, expiry, target_strike, selected.get("strike"), available[:12],
                )
                return None
            logger.warning(
                "MCX option exact strike missing; using nearest underlying=%s opt=%s expiry=%s target=%s selected=%s",
                underlying, option_type, expiry, target_strike, selected.get("strike"),
            )

        resolved = {
            "trading_symbol": selected["trading_symbol"],
            "tradingsymbol": selected["trading_symbol"],
            "instrument_token": selected["instrument_token"],
            "upstox_instrument_token": selected["instrument_token"],
            "expiry": selected["expiry"],
            "strike": float(selected["strike"]),
            "exchange": "MCX",
            "segment": "MCX_FO",
            "instrument_type": "OPTCOM",
            "option_type": selected["option_type"],
            "underlying": underlying,
            "lot_size": int(selected.get("lot_size") or 1),
            "spot": float(spot),
            "atm_strike": self._atm_strike(float(spot), int(strike_interval or 1)),
            "resolution_source": "upstox_mcx_master",
        }
        logger.info(
            "Resolved MCX option via master underlying=%s opt=%s expiry=%s strike=%s key=%s symbol=%s",
            underlying,
            option_type,
            resolved["expiry"],
            resolved["strike"],
            resolved["instrument_token"],
            resolved["trading_symbol"],
        )
        return resolved

    @staticmethod
    def _atm_strike(spot: float, interval: int) -> int:
        return int(round(float(spot) / max(1, interval)) * max(1, interval))

    def _target_strike(self, spot: float, interval: int, option_type: str, otm_points: int) -> int:
        atm = self._atm_strike(spot, interval)
        target = atm + int(otm_points or 0) if option_type == "CE" else atm - int(otm_points or 0)
        return self._atm_strike(float(target), interval)


async def mcx_instrument_refresh_loop(db, stop_event: asyncio.Event) -> None:
    resolver = MCXContractResolver(db)
    logger.info("MCX instrument refresh loop started")
    last_bod_refresh_date: Optional[str] = None
    last_intraday_refresh = 0.0
    while not stop_event.is_set():
        try:
            ist = _ist_now()
            market_open = _is_mcx_market_open()
            if ist.weekday() < 5 and ist.hour >= 6 and last_bod_refresh_date != ist.date().isoformat():
                status = await resolver.refresh(reason="daily-bod", force=True)
                if status.get("ok"):
                    last_bod_refresh_date = ist.date().isoformat()
            if market_open and time.monotonic() - last_intraday_refresh >= 30 * 60:
                await resolver.refresh(reason="intraday-30m", force=True)
                last_intraday_refresh = time.monotonic()
        except Exception as exc:
            logger.warning("MCX instrument refresh loop error: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
    logger.info("MCX instrument refresh loop stopped")
