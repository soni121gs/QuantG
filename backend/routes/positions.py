from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException

from core import db, get_current_user

router = APIRouter(tags=["Positions"])


@router.post("/positions/{symbol}/exit")
async def exit_position(symbol: str, user=Depends(get_current_user)):
    from server import (
        _place_order_core, ACTIVE_STRATEGY_POSITION_STATUSES,
        get_user_settings, _fetch_broker_positions_for_user,
    )
    symbol = symbol.upper()
    settings = await get_user_settings(user["id"])
    positions = await _fetch_broker_positions_for_user(user, settings)
    target = next((p for p in positions if p["symbol"] == symbol), None)
    if not target or not target.get("qty"):
        raise HTTPException(status_code=404, detail="No open position for that symbol")
    qty = abs(int(target["qty"]))
    side = "SELL" if target["qty"] > 0 else "BUY"
    # Real mark for the exit fill. _fetch_broker_positions_for_user stamps `ltp`
    # from the position's monitor-updated last_ltp; pass it so the MARKET exit
    # books at the live price instead of hitting the price-unavailable skip.
    exit_px = None
    try:
        _px = float(target.get("ltp") or 0)
        if _px > 0:
            exit_px = _px
    except (TypeError, ValueError):
        exit_px = None
    exchange = target.get("exchange") or ("NFO" if symbol.endswith(("CE", "PE")) else "NSE")
    instrument_token = str(target.get("instrument_token") or "").strip()
    if exchange in {"NFO", "BFO", "MCX"} or symbol.endswith(("CE", "PE")):
        if "|" not in instrument_token:
            strategy_pos = await db.strategy_positions.find_one(
                {
                    "user_id": user["id"],
                    "$or": [{"trading_symbol": symbol}, {"symbol": symbol}],
                    "status": {"$in": list(ACTIVE_STRATEGY_POSITION_STATUSES)},
                },
                {"_id": 0},
            )
            instrument_token = str((strategy_pos or {}).get("instrument_token") or "").strip()
        if "|" not in instrument_token:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot exit {symbol} from QuantG: Upstox instrument_key is missing. "
                    "Exit this position in Upstox now, then run Sync with Broker."
                ),
            )
        lot_size = int(target.get("lot_size") or qty or 1)
        return await _place_order_core(
            user_id=user["id"],
            symbol=symbol,
            side=side,
            qty=max(1, math.ceil(qty / max(1, lot_size))),
            order_type="MARKET",
            price=exit_px,
            product=target.get("product"),
            source="manual-exit",
            exchange=exchange,
            option_contract={
                "tradingsymbol": symbol,
                "exchange": exchange,
                "instrument_token": instrument_token,
                "lot_size": lot_size,
                "transaction_type": side,
                "ltp": exit_px or 0.0,
            },
            is_exit_order=True,
            exit_reason="manual-exit",
        )
    return await _place_order_core(
        user_id=user["id"], symbol=symbol, side=side, qty=qty,
        order_type="MARKET", price=exit_px, product=target.get("product"),
        source="manual-exit", exchange=exchange,
        is_exit_order=True, exit_reason="manual-exit",
    )


@router.get("/positions")
async def list_positions(user=Depends(get_current_user)):
    from server import get_user_settings, _fetch_broker_positions_for_user
    settings = await get_user_settings(user["id"])
    return await _fetch_broker_positions_for_user(user, settings)


@router.get("/execution/snapshot")
async def execution_snapshot(sync: bool = False, user=Depends(get_current_user)):
    from server import (
        execution_state_manager, market_session_snapshot,
        get_user_upstox_gateway, feed_health_status, broker_reconciliation_summary,
    )
    snapshot = await execution_state_manager.build_snapshot(user, sync=sync)
    snapshot["market_session"] = market_session_snapshot()
    try:
        gw = await get_user_upstox_gateway(user["id"])
        gateway_status = gw.status() if gw else {"connected": False, "feed_status": {"connected": False}}
        latest_ticks = gw.latest_ticks() if gw else {}
        snapshot["upstox_data_health"] = feed_health_status(gateway_status, latest_ticks)
        snapshot["broker_reconciliation"] = await broker_reconciliation_summary(db, user["id"], gw)
    except Exception as exc:
        snapshot["upstox_data_health"] = {"readiness": "UNKNOWN", "reason": str(exc)[:200]}
        snapshot["broker_reconciliation"] = {"status": "UNKNOWN", "errors": [str(exc)[:200]]}
    return snapshot
