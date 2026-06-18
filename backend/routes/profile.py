from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import db, get_current_user

router = APIRouter(tags=["Profile"])


class ProfileUpdateReq(BaseModel):
    name: Optional[str] = None
    default_qty: Optional[int] = None
    default_product: Optional[str] = None
    max_daily_loss: Optional[float] = None
    max_position_size: Optional[float] = None
    per_strategy_capital: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    data_broker: Optional[str] = None
    execution_broker: Optional[str] = None
    fallback_broker: Optional[str] = None
    paper_mode: Optional[bool] = None
    allow_simulated_prices: Optional[bool] = None


class ChangePasswordReq(BaseModel):
    current_password: str
    new_password: str


@router.get("/portfolio/holdings")
async def list_holdings(user=Depends(get_current_user)):
    return {
        "holdings": [],
        "source": "upstox",
        "message": "Holdings are not imported yet; QuantG live state uses Upstox positions and app orders.",
    }


@router.get("/portfolio")
async def portfolio(user=Depends(get_current_user)):
    from server import _fetch_broker_positions_for_user, _fill_ledger_summary, get_user_settings

    settings = await get_user_settings(user["id"])
    mode = "paper" if settings.get("paper_mode", True) else "live"
    positions = await _fetch_broker_positions_for_user(user, settings)
    open_pnl = round(sum(p["pnl"] for p in positions), 2)
    fill_summary = await _fill_ledger_summary(user["id"], mode=mode)
    charges = round(float(fill_summary.get("brokerage") or 0) + float(fill_summary.get("slippage") or 0), 2)
    gross_pnl = round(fill_summary["realized_pnl"] + charges + open_pnl, 2)
    total_pnl = round(fill_summary["realized_pnl"] + open_pnl, 2)
    deployed = round(sum(abs(p["qty"]) * p["avg_price"] for p in positions), 2)
    from core.paper_broker import PaperWallet as _PW
    _wallet_balance = await _PW(db).get_balance(user["id"])
    orders_count = await db.orders.count_documents({"user_id": user["id"]})
    strategies_count = await db.strategies.count_documents({"user_id": user["id"]})
    live_strategies = await db.strategies.count_documents({"user_id": user["id"], "status": "live"})
    paused_strategies = await db.strategies.count_documents({"user_id": user["id"], "status": "paused"})
    active_strategies = await db.strategies.count_documents({
        "user_id": user["id"],
        "status": {"$nin": ["live", "paused"]},
    })
    position_modes = sorted({p.get("mode", "unknown") for p in positions})

    equity = []
    base = 100000.0
    for i in range(30):
        d = datetime.now(timezone.utc) - timedelta(days=30 - i)
        wobble = math.sin((i + 1) * 0.63) * 450
        trend = i * 55
        equity.append({"date": d.strftime("%Y-%m-%d"), "equity": round(base + trend + wobble + total_pnl * (i / 30), 2)})

    return {
        "total_pnl": total_pnl,
        "gross_pnl": gross_pnl,
        "charges": charges,
        "net_pnl": total_pnl,
        "realized_pnl": fill_summary["realized_pnl"],
        "open_pnl": open_pnl,
        "pnl_type": "realized_plus_open",
        "realized_pnl_source": fill_summary["source"],
        "pnl_source": "none" if not position_modes else position_modes[0] if len(position_modes) == 1 else "mixed",
        "open_positions": len(positions),
        "deployed": deployed,
        "available": round(float(_wallet_balance or 0), 2),
        "orders": orders_count,
        "strategies": strategies_count,
        "live_strategies": live_strategies,
        "paused_strategies": paused_strategies,
        "active_strategies": active_strategies,
        "equity_curve": equity,
    }


@router.get("/funds")
async def funds(user=Depends(get_current_user)):
    from server import get_user_settings, get_user_upstox_gateway, logger

    settings = await get_user_settings(user["id"])
    execution_broker = settings.get("execution_broker", "upstox")
    paper_mode = bool(settings.get("paper_mode", True))

    if execution_broker == "upstox" and not paper_mode:
        upstox_gw = await get_user_upstox_gateway(user["id"])
        if upstox_gw and upstox_gw.connected:
            try:
                margins_payload = await asyncio.to_thread(upstox_gw.get_margins)
                if margins_payload and margins_payload.get("status") == "success":
                    data = margins_payload.get("data", {})
                    equity = data.get("equity", {})
                    commodity = data.get("commodity", {}) or data.get("commodities", {}) or {}
                    avail = round(
                        float(equity.get("available_margin") or 0)
                        + float(commodity.get("available_margin") or 0),
                        2,
                    )
                    used = round(
                        float(equity.get("used_margin") or 0)
                        + float(commodity.get("used_margin") or 0),
                        2,
                    )
                    payin = round(
                        float(equity.get("payin_amount") or 0)
                        + float(commodity.get("payin_amount") or 0),
                        2,
                    )
                    opening = round(avail + used - payin, 2)
                    return {
                        "source": "live",
                        "available_cash": avail,
                        "opening_balance": opening,
                        "intraday_payin": payin,
                        "used_margin": used,
                        "m2m_realized": 0.0,
                        "m2m_unrealized": 0.0,
                        "span": round(float(equity.get("span_margin") or 0) + float(commodity.get("span_margin") or 0), 2),
                        "delivery_margin": 0.0,
                        "raw_segments": {"equity": bool(equity), "commodity": bool(commodity)},
                    }
            except Exception as e:
                logger.warning(f"Upstox margins fetch failed: {e}")

    from core.paper_broker import PaperWallet
    _pw = PaperWallet(db)
    wallet_doc = await _pw.get_or_initialize(user["id"])
    paper_capital = float(wallet_doc.get("balance", 500000.0))
    initial_balance = float(wallet_doc.get("initial_balance", 500000.0))
    positions = await db.positions.find({"user_id": user["id"]}, {"_id": 0}).to_list(200)
    deployed = round(sum(abs(p.get("qty", 0)) * p.get("avg_price", 0) for p in positions), 2)
    open_sp = await db.strategy_positions.find(
        {"user_id": user["id"], "status": {"$in": ["OPEN", "FILLED"]}, "mode": "paper"},
        {"unrealized_pnl": 1, "unrealized_pnl": 1, "_id": 0},
    ).to_list(200)
    m2m_unrealized = round(sum(
        float(p.get("unrealized_pnl") or p.get("unrealized_pnl") or 0) for p in open_sp
    ), 2)
    return {
        "source": "paper",
        "available_cash": round(paper_capital, 2),
        "opening_balance": initial_balance,
        "intraday_payin": 0.0,
        "used_margin": deployed,
        "m2m_realized": round(paper_capital - initial_balance, 2),
        "m2m_unrealized": m2m_unrealized,
        "span": 0.0,
        "delivery_margin": 0.0,
        "note": "Paper-mode. Balance reflects actual fills from db.paper_wallets.",
    }


@router.get("/profile/paper-trading-stats")
async def paper_trading_stats(user=Depends(get_current_user)):
    trades = await db.paper_trading_history.find(
        {"user_id": user["id"]},
        {"_id": 0}
    ).sort("created_at", -1).to_list(1000)

    total_pnl = round(sum(float(t.get("pnl", 0)) for t in trades), 2)
    total_trades = sum(int(t.get("trades_count", 0)) for t in trades)
    total_wins = sum(int(t.get("wins", 0)) for t in trades)
    total_losses = sum(int(t.get("losses", 0)) for t in trades)

    return {
        "total_pnl": total_pnl,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "win_rate": round(total_wins / max(1, total_wins + total_losses) * 100, 2),
        "recent_backtests": trades[:10],
    }


@router.get("/profile")
async def get_profile(user=Depends(get_current_user)):
    from server import APP_VERSION, get_user_settings, get_user_upstox_status

    settings = await get_user_settings(user["id"])
    paper_stats = await paper_trading_stats(user=user)
    return {
        "id": user["id"],
        "email": user["email"],
        "created_at": user["created_at"],
        "role": user.get("role", "trader"),
        "version": APP_VERSION,
        **settings,
        "upstox": await get_user_upstox_status(user["id"]),
        "paper_trading_stats": paper_stats,
    }


@router.put("/profile")
async def update_profile(req: ProfileUpdateReq, user=Depends(get_current_user)):
    from server import _sync_strategy_modes_to_profile

    update = {k: v for k, v in req.model_dump().items() if v is not None}

    if "paper_mode" in update and update["paper_mode"] is False:
        update["allow_simulated_prices"] = False

    if "default_product" in update and update["default_product"] not in ("MIS", "CNC", "NRML"):
        raise HTTPException(status_code=400, detail="default_product must be MIS, CNC or NRML")
    if "default_qty" in update and update["default_qty"] <= 0:
        raise HTTPException(status_code=400, detail="default_qty must be > 0")
    for f in ("max_daily_loss", "max_position_size", "per_strategy_capital"):
        if f in update and update[f] < 0:
            raise HTTPException(status_code=400, detail=f"{f} cannot be negative")
    if "max_trades_per_day" in update and update["max_trades_per_day"] < 0:
        raise HTTPException(status_code=400, detail="max_trades_per_day cannot be negative")
    if "data_broker" in update and update["data_broker"] != "upstox":
        raise HTTPException(status_code=400, detail="data_broker must be upstox")
    if "execution_broker" in update and update["execution_broker"] != "upstox":
        raise HTTPException(status_code=400, detail="execution_broker must be upstox")
    if "fallback_broker" in update and update["fallback_broker"] not in {"none", "upstox"}:
        raise HTTPException(status_code=400, detail="fallback_broker must be none or upstox")
    if update:
        await db.users.update_one({"id": user["id"]}, {"$set": update})
        if "paper_mode" in update:
            await _sync_strategy_modes_to_profile(user["id"], bool(update["paper_mode"]))
    return await get_profile(user=user)


@router.post("/profile/reset-paper")
async def reset_paper_trading(user=Depends(get_current_user)):
    from server import ORDER_PAPER_FILLED, _closed_order_statuses, logger

    user_id = user["id"]
    logger.info(f"Initiating paper trading reset for user_id={user_id}")

    user_strategies = await db.strategies.find({"user_id": user_id}).to_list(1000)
    strategy_ids = [s["id"] for s in user_strategies]
    paper_strategy_ids = [
        s["id"] for s in user_strategies
        if str(s.get("mode") or "paper").lower() == "paper"
    ]
    paper_ledger_strategy_ids = paper_strategy_ids or strategy_ids

    terminal_paper_statuses = list(_closed_order_statuses() | {ORDER_PAPER_FILLED, "PAPER_FILLED", "COMPLETE", "FILLED", "CLOSED"})
    archived_orders_res = await db.orders.update_many(
        {"user_id": user_id, "mode": "paper", "status": {"$in": terminal_paper_statuses}},
        {"$set": {"orderbook_scope": "history", "history_preserved": True, "reset_archived_at": datetime.now(timezone.utc).isoformat()}},
    )
    orders_res = await db.orders.delete_many({
        "user_id": user_id,
        "mode": "paper",
        "status": {"$nin": terminal_paper_statuses},
    })

    sp_res = await db.strategy_positions.delete_many({"user_id": user_id, "mode": "paper"})

    pos_res = await db.positions.delete_many({
        "user_id": user_id,
        "$or": [
            {"mode": "paper"},
            {"broker": "paper"},
            {
                "mode": {"$exists": False},
                "broker_order_id": {"$in": [None, ""]},
                "strategy_id": {"$in": strategy_ids},
            },
        ],
    })

    locks_res = await db.strategy_position_locks.delete_many({
        "user_id": user_id,
        "strategy_id": {"$in": strategy_ids},
    })

    sig_res = await db.signals.delete_many({
        "user_id": user_id,
        "$or": [
            {"mode": "paper"},
            {
                "mode": {"$exists": False},
                "strategy_id": {"$in": paper_strategy_ids or strategy_ids},
                "status": {"$in": ["PENDING", "FILTERED", "REJECTED", "SKIPPED", "SKIPPED_SIGNAL"]},
            },
        ],
    })
    skipped_res = await db.skipped_signals.delete_many({"user_id": user_id})

    history_res = await db.paper_trading_history.update_many(
        {"user_id": user_id},
        {"$set": {"history_preserved": True, "reset_archived_at": datetime.now(timezone.utc).isoformat()}},
    )

    trades_res = await db.trades.update_many(
        {"user_id": user_id, "mode": "paper"},
        {"$set": {"history_preserved": True, "reset_archived_at": datetime.now(timezone.utc).isoformat()}},
    )
    fills_res = await db.trade_fills.update_many(
        {"user_id": user_id, "mode": "paper"},
        {"$set": {"history_preserved": True, "reset_archived_at": datetime.now(timezone.utc).isoformat()}},
    )

    open_opt_res = await db.option_open_positions.delete_many({"strategy_id": {"$in": paper_ledger_strategy_ids}})
    opt_pnl_res = await db.option_daily_pnl.delete_many({"strategy_id": {"$in": paper_ledger_strategy_ids}})
    opt_journal_res = await db.option_trade_journal.delete_many({"strategy_id": {"$in": paper_ledger_strategy_ids}})

    await db.option_strategy_states.update_many(
        {"strategy_id": {"$in": paper_ledger_strategy_ids}},
        {"$set": {"state": "IDLE", "cooldown_until": None}}
    )

    risk_events_res = await db.risk_events.delete_many({"user_id": user_id, "paper": True})

    from core.paper_broker import PaperWallet
    wallet = PaperWallet(db)
    wallet_doc = await wallet.reset(user_id)

    return {
        "ok": True,
        "detail": "Paper trading environment reset successfully. Live configurations intact.",
        "paper_wallet": {"balance": wallet_doc["balance"], "currency": "INR"},
        "purged": {
            "orders": orders_res.deleted_count,
            "orders_archived": archived_orders_res.modified_count,
            "strategy_positions": sp_res.deleted_count,
            "broker_positions": pos_res.deleted_count,
            "position_locks": locks_res.deleted_count,
            "signals": sig_res.deleted_count,
            "skipped_signals": skipped_res.deleted_count,
            "stats_history": history_res.modified_count,
            "closed_trades": trades_res.modified_count,
            "trade_fills": fills_res.modified_count,
            "option_open_positions": open_opt_res.deleted_count,
            "option_daily_pnl": opt_pnl_res.deleted_count,
            "option_trade_journal": opt_journal_res.deleted_count,
            "risk_events": risk_events_res.deleted_count,
        }
    }


async def _recover_paper_contract_resolution_halts_for_user(user_id: str) -> Dict[str, Any]:
    from server import _sync_strategy_modes_to_profile, get_user_settings

    settings = await get_user_settings(user_id)
    mode_synced = 0
    if bool(settings.get("paper_mode", True)):
        mode_synced = await _sync_strategy_modes_to_profile(user_id, True)
    now = datetime.now(timezone.utc).isoformat()
    query = {
        "user_id": user_id,
        "mode": "paper",
        "$or": [
            {"halt_reason": "CONTRACT_RESOLUTION_FAILED"},
            {"last_error": {"$regex": "CONTRACT_RESOLUTION_FAILED|contract resolution failed|contract unresolved|option contract resolution failed", "$options": "i"}},
            {"last_filter_reason": {"$regex": "CONTRACT_RESOLUTION_FAILED|contract resolution failed|contract unresolved|option contract resolution failed", "$options": "i"}},
        ],
    }
    rows = await db.strategies.find(query, {"_id": 0, "id": 1, "name": 1, "status": 1}).to_list(500)
    res = await db.strategies.update_many(
        query,
        {
            "$set": {
                "halted": False,
                "is_halted": False,
                "last_error": "",
                "last_filter_reason": "Recovered paper contract-resolution halt; runner will rescan.",
                "paper_contract_recovered_at": now,
            },
            "$unset": {
                "halt_reason": "",
                "last_halt_reason": "",
            },
        },
    )
    return {
        "ok": True,
        "matched": res.matched_count,
        "modified": res.modified_count,
        "mode_synced": mode_synced,
        "strategies": rows,
        "recovered_at": now,
    }


@router.post("/profile/recover-paper-contract-halts")
async def recover_paper_contract_halts(user=Depends(get_current_user)):
    return await _recover_paper_contract_resolution_halts_for_user(user["id"])


@router.get("/paper-wallet")
async def get_paper_wallet(user=Depends(get_current_user)):
    from core.paper_broker import PaperWallet

    pw = PaperWallet(db)
    wallet = await pw.get_or_initialize(user["id"])
    summary = pw.summary(wallet)
    recent_fills = await db.trade_fills.find(
        {"user_id": user["id"], "mode": "paper"},
        {"_id": 0, "id": 1, "side": 1, "symbol": 1, "target_symbol": 1,
         "qty": 1, "price": 1, "trade_value": 1, "brokerage": 1, "created_at": 1}
    ).sort("created_at", -1).limit(10).to_list(10)
    return {
        "ok": True,
        "currency": "INR",
        **summary,
        "recent_fills": recent_fills,
    }


@router.post("/profile/change-password")
async def change_password(req: ChangePasswordReq, user=Depends(get_current_user)):
    from server import hash_password, verify_password

    full = await db.users.find_one({"id": user["id"]})
    if not full or not verify_password(req.current_password, full["password_hash"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    await db.users.update_one({"id": user["id"]}, {"$set": {"password_hash": hash_password(req.new_password)}})
    return {"changed": True}
