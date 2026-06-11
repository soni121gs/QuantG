from __future__ import annotations

from typing import Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core import db, get_current_user

router = APIRouter(prefix="/orders", tags=["Orders"])


class OrderReq(BaseModel):
    symbol: str
    side: str
    qty: int = Field(gt=0, description="Quantity must be > 0")
    order_type: str = "MARKET"
    price: Optional[float] = None
    product: str = "MIS"
    exchange: str = "NSE"
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=160)


@router.post("")
async def place_order(req: OrderReq, user=Depends(get_current_user)):
    from server import _place_order_core
    return await _place_order_core(
        user_id=user["id"], symbol=req.symbol, side=req.side, qty=req.qty,
        order_type=req.order_type, price=req.price, product=req.product, source="manual",
        exchange=req.exchange,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
        idempotency_key=req.idempotency_key,
    )


@router.get("")
async def list_orders(include_stale: bool = False, user=Depends(get_current_user)):
    from server import _sync_upstox_order_statuses, _sync_strategy_positions_with_broker, STALE_ORDER_STATUSES
    await _sync_upstox_order_statuses(user["id"])
    await _sync_strategy_positions_with_broker(user["id"])
    order_query: Dict[str, Any] = {"user_id": user["id"]}
    if not include_stale:
        order_query["status"] = {"$nin": list(STALE_ORDER_STATUSES)}
        order_query["visibility"] = {"$ne": "hidden"}
    rows = await db.orders.find(order_query, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows
