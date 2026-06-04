"""Upstox trading-quality helpers for instruments, data health, and broker truth.

The functions in this module are deliberately small and dependency-light so the
live order path can call them without pulling UI or strategy code into broker
adapters.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import os
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from pymongo import ReplaceOne


logger = logging.getLogger("quantg.upstox_quality")


UPSTOX_INSTRUMENT_URLS = {
    "complete": os.environ.get("UPSTOX_INSTRUMENTS_COMPLETE_URL", "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"),
    "nse": os.environ.get("UPSTOX_INSTRUMENTS_NSE_URL", "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"),
    "bse": os.environ.get("UPSTOX_INSTRUMENTS_BSE_URL", "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz"),
    "mcx": os.environ.get("UPSTOX_INSTRUMENTS_MCX_URL", "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"),
    "suspended": os.environ.get("UPSTOX_SUSPENDED_INSTRUMENTS_URL", "https://assets.upstox.com/market-quote/instruments/exchange/suspended.json.gz"),
}

UPSTOX_INSTRUMENT_SYNC_TTL_SEC = int(os.environ.get("UPSTOX_INSTRUMENT_SYNC_TTL_SEC", str(24 * 3600)))
QUOTE_STALE_SECONDS = float(os.environ.get("UPSTOX_QUOTE_STALE_SECONDS", "30"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric /= 1000
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except Exception:
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def quote_age_seconds(value: Any) -> Optional[float]:
    parsed = parse_dt(value)
    if not parsed:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def is_quote_stale(value: Any, *, threshold_sec: float = QUOTE_STALE_SECONDS) -> bool:
    age = quote_age_seconds(value)
    return age is None or age > threshold_sec


def fetch_json_url(url: str, timeout: float = 30.0) -> List[Dict[str, Any]]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        raw = response.read()
    if url.endswith(".gz"):
        raw = gzip.decompress(raw)
    payload = json.loads(raw.decode("utf-8"))
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("instruments") or []
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    return []


def normalize_instrument_row(row: Dict[str, Any], *, source: str, suspended: bool = False) -> Optional[Dict[str, Any]]:
    key = str(row.get("instrument_key") or row.get("instrument_token") or "").strip()
    if "|" not in key:
        return None
    now = utc_now_iso()
    return {
        "_id": key,
        "instrument_key": key,
        "exchange_token": str(row.get("exchange_token") or ""),
        "trading_symbol": row.get("trading_symbol") or row.get("tradingsymbol") or row.get("symbol") or "",
        "tradingsymbol": row.get("tradingsymbol") or row.get("trading_symbol") or row.get("symbol") or "",
        "name": row.get("name") or row.get("short_name") or "",
        "exchange": row.get("exchange") or "",
        "segment": row.get("segment") or row.get("exchange_segment") or "",
        "instrument_type": row.get("instrument_type") or "",
        "option_type": row.get("option_type") or "",
        "expiry": row.get("expiry") or row.get("expiry_date"),
        "strike": row.get("strike") or row.get("strike_price"),
        "lot_size": row.get("lot_size") or row.get("minimum_lot"),
        "tick_size": row.get("tick_size"),
        "raw": row,
        "source": source,
        "suspended": bool(suspended),
        "updated_at": now,
    }


async def sync_upstox_instruments(db: Any, *, force: bool = False, sources: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    selected = list(sources or UPSTOX_INSTRUMENT_URLS.keys())
    meta = await db.upstox_instrument_sync_meta.find_one({"_id": "daily-json"}, {"_id": 0})
    if not force and meta and meta.get("completed_at"):
        age = quote_age_seconds(meta.get("completed_at"))
        if age is not None and age < UPSTOX_INSTRUMENT_SYNC_TTL_SEC:
            return {"ok": True, "refreshed": False, "reason": "fresh", "meta": meta}

    started = utc_now_iso()
    totals: Dict[str, Any] = {"ok": True, "refreshed": True, "started_at": started, "sources": {}}
    for source in selected:
        url = UPSTOX_INSTRUMENT_URLS.get(source)
        if not url:
            continue
        suspended = source == "suspended"
        try:
            rows = await asyncio.to_thread(fetch_json_url, url)
            normalized = [normalize_instrument_row(row, source=source, suspended=suspended) for row in rows]
            docs = [doc for doc in normalized if doc]
            if docs:
                ops = [ReplaceOne({"_id": doc["_id"]}, doc, upsert=True) for doc in docs]
                await db.upstox_instruments.bulk_write(ops, ordered=False)
                if suspended:
                    suspended_ops = [
                        ReplaceOne({"_id": doc["_id"]}, {"_id": doc["_id"], "instrument_key": doc["_id"], "updated_at": doc["updated_at"], "raw": doc["raw"]}, upsert=True)
                        for doc in docs
                    ]
                    await db.upstox_suspended_instruments.bulk_write(suspended_ops, ordered=False)
            totals["sources"][source] = {"ok": True, "fetched": len(rows), "stored": len(docs)}
        except Exception as exc:
            totals["ok"] = False
            totals["sources"][source] = {"ok": False, "error": str(exc)[:500]}
            logger.warning("Upstox instrument sync failed source=%s url=%s: %s", source, url, exc)

    totals["completed_at"] = utc_now_iso()
    await db.upstox_instrument_sync_meta.update_one({"_id": "daily-json"}, {"$set": totals}, upsert=True)
    return totals


async def lookup_instrument_guard(db: Any, instrument_key: str) -> Dict[str, Any]:
    key = str(instrument_key or "").strip()
    if "|" not in key:
        return {"ok": False, "reason_code": "INSTRUMENT_KEY_INVALID", "reason": "Upstox instrument_key is missing or not in segment|token form."}
    doc = await db.upstox_instruments.find_one({"_id": key}, {"_id": 0})
    suspended = await db.upstox_suspended_instruments.find_one({"_id": key}, {"_id": 0})
    if suspended or (doc and doc.get("suspended")):
        return {"ok": False, "reason_code": "INSTRUMENT_SUSPENDED", "reason": "Upstox suspended instruments list blocks this contract.", "instrument": doc, "suspended": suspended}
    if doc:
        return {"ok": True, "instrument": doc}
    return {"ok": False, "reason_code": "INSTRUMENT_MASTER_MISSING", "reason": "Instrument was not found in the synced Upstox JSON master."}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _score(value: float, worst: float, best: float, *, inverse: bool = False) -> int:
    if best == worst:
        return 0
    if inverse:
        ratio = (worst - value) / (worst - best)
    else:
        ratio = (value - worst) / (best - worst)
    return int(max(0, min(100, round(ratio * 100))))


def option_entry_quality_score(
    contract: Dict[str, Any],
    *,
    spot: Optional[float] = None,
    chain_row: Optional[Dict[str, Any]] = None,
    quote: Optional[Dict[str, Any]] = None,
    pcr: Optional[float] = None,
) -> Dict[str, Any]:
    chain_row = chain_row or {}
    quote = quote or {}
    strike = _float(contract.get("strike") or chain_row.get("strike_price"))
    spot_val = _float(spot or contract.get("spot"))
    premium = _float(contract.get("ltp") or quote.get("ltp") or quote.get("last_price"))
    bid = _float(quote.get("bid") or quote.get("bid_price") or quote.get("best_bid_price") or chain_row.get("bid_price"))
    ask = _float(quote.get("ask") or quote.get("ask_price") or quote.get("best_ask_price") or chain_row.get("ask_price"))
    spread_pct = 100.0
    if bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2
        spread_pct = ((ask - bid) / max(mid, 0.01)) * 100
    volume = _float(quote.get("volume") or quote.get("vtt") or chain_row.get("volume"))
    oi = _float(quote.get("oi") or chain_row.get("oi"))
    oi_change = _float(quote.get("oi_change") or quote.get("change_oi") or chain_row.get("oi_change"))
    age = quote_age_seconds(quote.get("timestamp") or quote.get("received_at") or contract.get("received_at"))

    atm_distance_pct = 100.0
    if strike > 0 and spot_val > 0:
        atm_distance_pct = abs(strike - spot_val) / spot_val * 100

    pcr_val = _float(pcr if pcr is not None else contract.get("pcr"), default=0.0)
    components = {
        "atm_distance": _score(atm_distance_pct, 6.0, 0.0, inverse=True),
        "premium_range": 100 if 5 <= premium <= 500 else 65 if 1 <= premium <= 1000 else 20,
        "spread_quality": _score(spread_pct, 12.0, 0.5, inverse=True),
        "volume": _score(math.log10(max(volume, 1)), 0.0, 5.0),
        "oi": _score(math.log10(max(oi + abs(oi_change), 1)), 0.0, 6.0),
        "pcr": 100 if 0.6 <= pcr_val <= 1.4 else 65 if 0.35 <= pcr_val <= 2.0 else 35,
        "quote_freshness": 100 if age is not None and age <= 5 else 75 if age is not None and age <= 15 else 35 if age is not None and age <= QUOTE_STALE_SECONDS else 0,
    }
    weights = {
        "atm_distance": 0.18,
        "premium_range": 0.12,
        "spread_quality": 0.20,
        "volume": 0.14,
        "oi": 0.12,
        "pcr": 0.10,
        "quote_freshness": 0.14,
    }
    score = int(round(sum(components[k] * weights[k] for k in weights)))
    warnings = []
    if age is None or age > QUOTE_STALE_SECONDS:
        warnings.append("QUOTE_STALE")
    if spread_pct > 5:
        warnings.append("WIDE_SPREAD")
    if volume <= 0:
        warnings.append("LOW_VOLUME")
    if premium <= 0:
        warnings.append("MISSING_PREMIUM")
    return {
        "score": max(0, min(100, score)),
        "grade": "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 45 else "D",
        "components": components,
        "metrics": {
            "atm_distance_pct": round(atm_distance_pct, 3) if atm_distance_pct != 100.0 else None,
            "premium": premium or None,
            "bid": bid or None,
            "ask": ask or None,
            "spread_pct": round(spread_pct, 3) if spread_pct != 100.0 else None,
            "volume": volume,
            "oi": oi,
            "oi_change": oi_change,
            "pcr": pcr_val or None,
            "quote_age_sec": round(age, 3) if age is not None else None,
        },
        "warnings": warnings,
        "readiness": "PASS" if score >= 65 and "QUOTE_STALE" not in warnings else "WARN" if score >= 45 else "BLOCK",
    }


def feed_health_status(gateway_status: Dict[str, Any], latest_ticks: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    feed_status = gateway_status.get("feed_status") or {}
    ages = []
    latest_tick_at = feed_status.get("last_tick_time") or gateway_status.get("last_tick_at")
    for tick in latest_ticks.values():
        age = quote_age_seconds(tick.get("received_at") or tick.get("timestamp"))
        if age is not None:
            ages.append(age)
        if not latest_tick_at:
            latest_tick_at = tick.get("received_at") or tick.get("timestamp")
    quote_age = min(ages) if ages else None
    connected = bool(feed_status.get("connected") or gateway_status.get("ws_running"))
    snapshot_received = bool(latest_ticks)
    stale = quote_age is None or quote_age > QUOTE_STALE_SECONDS
    return {
        "connected": connected,
        "snapshot_received": snapshot_received,
        "latest_tick_time": latest_tick_at,
        "quote_age_sec": round(quote_age, 3) if quote_age is not None else None,
        "quote_stale": stale,
        "market_status": feed_status.get("market_status") or {},
        "subscribed_count": feed_status.get("subscribed_count") or gateway_status.get("subscribed_tokens") or 0,
        "subscribed_modes": feed_status.get("subscribed_modes") or {},
        "state": feed_status.get("state") or ("connected" if connected else "disconnected"),
        "readiness": "READY" if connected and snapshot_received and not stale else "DEGRADED" if connected else "BLOCKED",
        "reason": None if connected and snapshot_received and not stale else ("quote stale or missing" if connected else "feed disconnected"),
    }


async def store_skipped_signal(db: Any, *, user_id: str, strategy_id: Optional[str], symbol: str, reason_code: str, reason: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    now = utc_now_iso()
    session_date = now[:10]
    dedupe_key = f"{user_id}:{strategy_id or 'manual'}:{symbol}:{reason_code}:{session_date}"
    update = {
        "$set": {
            "user_id": user_id,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "reason_code": reason_code,
            "reason": reason,
            "details": details or {},
            "session_date": session_date,
            "last_seen_at": now,
        },
        "$setOnInsert": {"id": dedupe_key, "first_seen_at": now},
        "$inc": {"count": 1},
    }
    await db.skipped_signals.update_one({"user_id": user_id, "dedupe_key": dedupe_key}, update, upsert=True)
    return {"id": dedupe_key, "status": "SKIPPED_SIGNAL", "reason_code": reason_code, "reason": reason}


async def apply_broker_truth_event(db: Any, payload: Dict[str, Any], *, source: str = "webhook") -> Dict[str, Any]:
    now = utc_now_iso()
    event_id = str(payload.get("order_id") or payload.get("gtt_order_id") or payload.get("instrument_token") or payload.get("instrument_key") or f"{source}:{time.time_ns()}")
    await db.upstox_broker_events.insert_one({
        "id": f"{source}:{event_id}:{time.time_ns()}",
        "source": source,
        "event_id": event_id,
        "payload": payload,
        "created_at": now,
    })
    order_id = str(payload.get("order_id") or payload.get("broker_order_id") or "")
    raw_status = str(payload.get("status") or payload.get("order_status") or payload.get("state") or "").upper()
    status_map = {
        "COMPLETE": "FILLED",
        "COMPLETED": "FILLED",
        "TRADED": "FILLED",
        "FILLED": "FILLED",
        "PARTIAL": "PARTIAL_FILL",
        "PARTIALLY_FILLED": "PARTIAL_FILL",
        "OPEN": "OPEN",
        "PENDING": "OPEN",
        "REJECTED": "REJECTED",
        "CANCELLED": "CANCELLED",
        "CANCELED": "CANCELLED",
    }
    canonical = status_map.get(raw_status)
    updated = 0
    if order_id and canonical:
        set_doc = {
            "status": canonical,
            "execution_status": canonical,
            "legacy_status": raw_status,
            "broker_status_message": payload.get("status_message") or raw_status,
            "broker_truth_source": source,
            "broker_truth_at": now,
            "updated_at": now,
        }
        if canonical == "FILLED":
            qty = payload.get("filled_quantity") or payload.get("quantity")
            price = payload.get("average_price") or payload.get("price")
            if qty is not None:
                set_doc["filled_qty"] = int(float(qty or 0))
                set_doc["pending_qty"] = 0
            if price not in (None, ""):
                set_doc["average_price"] = float(price)
        res = await db.orders.update_many({"broker_order_id": order_id}, {"$set": set_doc})
        updated = int(res.modified_count or 0)
    return {"ok": True, "event_id": event_id, "order_id": order_id or None, "status": canonical, "updated_orders": updated}


async def broker_reconciliation_summary(db: Any, user_id: str, gateway: Any = None) -> Dict[str, Any]:
    now = utc_now_iso()
    local_orders = await db.orders.find({"user_id": user_id, "mode": "live"}, {"_id": 0}).sort("created_at", -1).limit(200).to_list(200)
    local_positions = await db.strategy_positions.find({"user_id": user_id, "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]}}, {"_id": 0}).to_list(200)
    broker_orders: List[Dict[str, Any]] = []
    broker_positions: List[Dict[str, Any]] = []
    errors = []
    if gateway:
        try:
            broker_orders = (await asyncio.to_thread(gateway.get_order_book)).get("orders") or []
        except Exception as exc:
            errors.append(f"order_book:{exc}")
        try:
            broker_positions = (await asyncio.to_thread(gateway.get_positions)).get("net") or []
        except Exception as exc:
            errors.append(f"positions:{exc}")
    broker_order_ids = {str(row.get("broker_order_id") or row.get("order_id") or "") for row in broker_orders}
    pending_without_broker = [
        row for row in local_orders
        if str(row.get("status") or "").upper() == "PENDING_BROKER" and str(row.get("broker_order_id") or "") not in broker_order_ids
    ]
    local_keys = {str(row.get("instrument_key") or row.get("active_instrument_key") or row.get("instrument_token") or "") for row in local_positions}
    broker_keys = {str(row.get("instrument_key") or row.get("instrument_token") or "") for row in broker_positions}
    mismatches = {
        "pending_orders_without_broker_match": len(pending_without_broker),
        "local_positions": len(local_positions),
        "broker_positions": len(broker_positions),
        "position_key_mismatches": len([key for key in local_keys if key and key not in broker_keys]) if broker_keys else 0,
    }
    status = "OK" if not errors and not pending_without_broker and mismatches["position_key_mismatches"] == 0 else "REVIEW"
    summary = {"generated_at": now, "status": status, "mismatches": mismatches, "errors": errors}
    await db.upstox_reconciliation_state.update_one({"_id": f"user:{user_id}"}, {"$set": summary}, upsert=True)
    return summary
