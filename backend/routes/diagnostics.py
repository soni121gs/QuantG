from __future__ import annotations

from fastapi import APIRouter, Depends

from core import db, get_current_user

router = APIRouter(tags=["Diagnostics"])


@router.get("/debug/position-integrity")
async def position_integrity_report(user=Depends(get_current_user)):
    from server import _fetch_broker_positions_for_user, get_user_settings

    user_id = user["id"]
    settings = await get_user_settings(user_id)

    broker_positions = await _fetch_broker_positions_for_user(user, settings)
    broker_active = [p for p in broker_positions if int(p.get("qty") or p.get("quantity") or 0) != 0]

    active_sp = await db.strategy_positions.find({
        "user_id": user_id,
        "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
    }).to_list(1000)

    active_sp_symbols = {str(sp.get("symbol")).upper(): sp for sp in active_sp if sp.get("symbol")}
    active_sp_strategy_ids = {str(sp.get("strategy_id")) for sp in active_sp if sp.get("strategy_id")}

    orphan_positions = 0
    missing_sl = 0
    missing_tp = 0
    strategy_mismatches = 0

    for bp in broker_active:
        symbol = str(bp.get("symbol") or "").upper()
        strategy_id = bp.get("strategy_id")

        is_orphan = True
        if strategy_id and str(strategy_id) in active_sp_strategy_ids:
            is_orphan = False
        elif symbol in active_sp_symbols:
            is_orphan = False

        if is_orphan:
            orphan_positions += 1

    for sp in active_sp:
        symbol = str(sp.get("symbol") or "").upper()
        tp_sl = sp.get("tp_sl_tsl_config") or {}

        has_sl = tp_sl.get("stop_loss") is not None or tp_sl.get("stoploss_price") is not None
        has_tp = tp_sl.get("take_profit") is not None or tp_sl.get("target_price") is not None

        if not has_sl:
            missing_sl += 1
        if not has_tp:
            missing_tp += 1

        bp_match = next((p for p in broker_active if str(p.get("symbol")).upper() == symbol), None)
        if not bp_match:
            strategy_mismatches += 1
        else:
            bp_qty = abs(int(bp_match.get("qty") or bp_match.get("quantity") or 0))
            sp_qty = int(sp.get("open_quantity") or sp.get("quantity") or 0)
            if bp_qty != sp_qty:
                strategy_mismatches += 1

    failed_orders_count = await db.orders.count_documents({
        "user_id": user_id,
        "status": "FAILED",
    })

    return {
        "total_positions": len(broker_active),
        "orphan_positions": orphan_positions,
        "missing_sl": missing_sl,
        "missing_tp": missing_tp,
        "strategy_mismatches": strategy_mismatches,
        "failed_orders": failed_orders_count,
    }
