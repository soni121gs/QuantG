from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException

from core import db, get_current_user

router = APIRouter(tags=["Positions"])


@router.post("/positions/{symbol}/exit")
async def exit_position(symbol: str, user=Depends(get_current_user)):
    from server import _close_strategy_positions, ACTIVE_STRATEGY_POSITION_STATUSES
    symbol = symbol.upper()
    # Close the live position for this symbol through the SAME proven path the
    # position monitor and strategy exit-all use. The old path placed a generic
    # SELL/BUY order under a "manual_recovery" strategy bucket, but the portfolio
    # ledger nets fills by (strategy_id, target_symbol): an exit whose strategy_id
    # did not match the position's therefore created a phantom SHORT instead of
    # closing the LONG (and equity/single-leg exits skipped entirely because
    # paper_ltp resolved to 0 during market hours without is_exit_order).
    # _close_strategy_positions closes the position it actually finds — correct
    # netting, real-mark pricing, wallet credit, spreads and equity alike, and it
    # works even for an orphaned position whose strategy doc no longer exists.
    pos = await db.strategy_positions.find_one(
        {
            "user_id": user["id"],
            "$or": [
                {"trading_symbol": symbol},
                {"symbol": symbol},
                {"target_symbol": symbol},
            ],
            "status": {"$in": list(ACTIVE_STRATEGY_POSITION_STATUSES)},
        },
        {"_id": 0, "strategy_id": 1},
    )
    if not pos or not pos.get("strategy_id"):
        raise HTTPException(status_code=404, detail="No open position for that symbol")
    result = await _close_strategy_positions(
        user["id"], pos["strategy_id"], reason="manual-exit"
    )
    return {"ok": True, "symbol": symbol, "closed": result}


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
