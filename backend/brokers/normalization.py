"""Shared normalization utilities for broker responses in QuantG.

Decouples payload normalization from specific broker clients (like Zerodha Kite Connect).
"""
from __future__ import annotations
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger("quantg.brokers.normalization")


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
    avg = float(average_price if average_price is not None else _first_value(raw, ["average_price", "avg_price", "averagePrice", "avgPrc", "buy_price", "buyPrice", "day_buy_price", "dayBuyPrice"]) or 0)
    ltp = float(last_price if last_price is not None else _first_value(raw, ["last_price", "ltp", "lastTradedPrice", "last_traded_price"]) or avg or 0)
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


def normalize_order_book(orders: Optional[List[Dict[str, Any]]], *, broker: str = "zerodha") -> List[Dict[str, Any]]:
    return [normalize_order_update(row, broker=broker) for row in (orders or []) if isinstance(row, dict)]
