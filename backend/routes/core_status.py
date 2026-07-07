from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, validator

from core import db, get_current_user
from core.backtest_engine import BacktestEngine
from core.performance_tracker import PerformanceTracker

router = APIRouter(tags=["Core Status"])


class BacktestReq(BaseModel):
    strategy_id: Optional[str] = None
    python_code: Optional[str] = None
    symbol: str = "RELIANCE"
    days: int = 60
    options: Optional[Dict[str, Any]] = None
    engine: str = "local"

    @validator("days")
    def clamp_days(cls, v: int) -> int:
        if v < 1:
            return 1
        return min(v, 365)


class LiveActivateReq(BaseModel):
    strategy_id: str
    acknowledge_real_money: bool = False


def _env_true(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() == "true"


def _strategy_structure(strategy: Dict[str, Any]) -> str:
    opts = (strategy.get("visual_config") or {}).get("options") or {}
    return str(strategy.get("structure") or opts.get("structure") or "").lower()


async def _selected_strategy_margin(gw: Any, strategy: Dict[str, Any]) -> Dict[str, Any]:
    from routes.strategies import (
        _margin_estimate_for_combo,
        _margin_est_option_types,
        _margin_est_structure,
        _margin_est_underlying,
        _margin_est_width_strikes,
    )

    underlying = _margin_est_underlying(strategy)
    structure = _margin_est_structure(strategy)
    opts = (strategy.get("visual_config") or {}).get("options") or {}
    try:
        short_otm_pct = float(opts.get("short_otm_pct") or 0)
    except (TypeError, ValueError):
        short_otm_pct = 0.0
    try:
        short_offset_strikes = int(opts.get("short_offset_strikes") or 0)
    except (TypeError, ValueError):
        short_offset_strikes = 0
    if not underlying:
        return {"ok": False, "error": "strategy underlying could not be resolved"}
    return await _margin_estimate_for_combo(
        gw,
        underlying,
        structure,
        width_strikes=_margin_est_width_strikes(strategy),
        short_otm_pct=short_otm_pct,
        short_offset_strikes=short_offset_strikes,
        option_types=_margin_est_option_types(strategy, structure),
    )


async def _live_activation_preflight(user_id: str, strategy_id: str) -> Dict[str, Any]:
    from server import (
        _extract_available_margin,
        broker_reconciliation_summary,
        get_user_upstox_gateway,
        get_user_upstox_status,
    )

    blockers: list[str] = []
    warnings: list[str] = []
    strategy = await db.strategies.find_one({"id": strategy_id, "user_id": user_id}, {"_id": 0})
    if not strategy:
        blockers.append("Selected strategy not found.")
        return {"ok": False, "blockers": blockers, "warnings": warnings}

    if str(strategy.get("status") or "").lower() == "archived":
        blockers.append("Selected strategy is archived.")

    structure = _strategy_structure(strategy)
    if structure != "credit_spread":
        blockers.append("Only credit_spread strategies are live-capable through this one-click pilot path.")

    if not _env_true("CORE_ENGINE_LIVE_ENABLED"):
        blockers.append("Server live engine is disabled: CORE_ENGINE_LIVE_ENABLED=false.")
    if structure == "credit_spread" and not _env_true("LIVE_SPREADS_ENABLED"):
        blockers.append("Live spread execution is disabled: LIVE_SPREADS_ENABLED=false.")

    kill_switch = await db.risk_state.find_one({"_id": "global_kill_switch"})
    if kill_switch and kill_switch.get("active"):
        blockers.append("Global kill-switch is active.")

    unknown_orders = await db.orders.count_documents({"user_id": user_id, "status": "UNKNOWN_NEEDS_REVIEW"})
    if unknown_orders:
        blockers.append(f"{unknown_orders} order(s) need manual reconciliation before live activation.")

    open_live_positions = await db.strategy_positions.count_documents({
        "user_id": user_id,
        "mode": "live",
        "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
    })
    if open_live_positions:
        blockers.append("A live position is already open or closing.")

    other_live = await db.strategies.count_documents({
        "user_id": user_id,
        "id": {"$ne": strategy_id},
        "mode": "live",
        "status": {"$nin": ["archived", "draft"]},
    })
    if other_live:
        blockers.append("Another strategy is already configured for live mode.")

    upstox_status = await get_user_upstox_status(user_id)
    if not upstox_status.get("keys_saved"):
        blockers.append("Upstox credentials are not saved.")
    if not (upstox_status.get("connected") and upstox_status.get("token_valid")):
        blockers.append("Upstox OAuth token is not valid. Reconnect Upstox first.")
    if not (upstox_status.get("gateway") or {}).get("ws_running") and not (upstox_status.get("feed_status") or {}).get("connected"):
        warnings.append("Upstox realtime feed is not confirmed running yet.")

    gw = await get_user_upstox_gateway(user_id)
    funds_payload: Dict[str, Any] = {}
    available_margin = 0.0
    required_margin = 0.0
    margin_estimate: Dict[str, Any] = {}
    if not gw or not getattr(gw, "connected", False):
        blockers.append("Upstox gateway is not connected.")
    else:
        try:
            funds_payload, margin_estimate = await asyncio.gather(
                asyncio.to_thread(gw.get_margins),
                _selected_strategy_margin(gw, strategy),
            )
            available_margin = _extract_available_margin(
                funds_payload,
                ((strategy.get("visual_config") or {}).get("exchange")
                 or strategy.get("instrument_group")
                 or "NSE_FO"),
            )
            if margin_estimate.get("ok"):
                required_margin = float(margin_estimate.get("required_margin") or 0)
            else:
                blockers.append(f"Could not calculate broker margin: {margin_estimate.get('error') or 'unknown error'}.")
        except Exception as exc:
            blockers.append(f"Live funds/margin check failed: {str(exc)[:220]}")
        if required_margin > 0:
            needed = required_margin * 1.15
            if available_margin < needed:
                blockers.append(
                    f"Insufficient Upstox margin buffer: need at least INR {needed:,.2f} "
                    f"(margin INR {required_margin:,.2f} + 15% buffer), available INR {available_margin:,.2f}."
                )

    try:
        reconciliation = await broker_reconciliation_summary(db, user_id, gw)
        if str(reconciliation.get("status") or "").upper() not in {"OK", "READY", "NO_GATEWAY"} or reconciliation.get("errors"):
            blockers.append(f"Broker reconciliation is not clean: {reconciliation.get('status')}.")
    except Exception as exc:
        blockers.append(f"Broker reconciliation check failed: {str(exc)[:220]}")

    return {
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "strategy": {
            "id": strategy.get("id"),
            "name": strategy.get("name"),
            "status": strategy.get("status"),
            "mode": strategy.get("mode"),
            "structure": structure,
        },
        "available_margin": round(available_margin, 2),
        "required_margin": round(required_margin, 2),
        "margin_estimate": margin_estimate if margin_estimate else None,
    }


@router.get("/core/strategies")
async def get_core_strategies(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.strategies.find({"user_id": user_id}, {"_id": 0}).to_list(length=200)


@router.get("/core/orders")
async def get_core_orders(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.orders.find({"user_id": user_id}, {"_id": 0}).sort("created_at", -1).to_list(length=200)


@router.get("/core/positions")
async def get_core_positions(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.strategy_positions.find({"user_id": user_id}, {"_id": 0}).to_list(length=200)


@router.get("/core/performance")
async def get_core_performance(user=Depends(get_current_user)):
    user_id = user["id"]
    tracker = PerformanceTracker(db)
    return await tracker.rebuild_leaderboard(user_id)


@router.get("/core/backtests")
async def get_core_backtests(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.backtest_runs.find({}, {"_id": 0}).sort("created_at", -1).to_list(length=100)


@router.post("/core/backtests/run")
async def run_core_backtest(req: BacktestReq, user=Depends(get_current_user)):
    from server import _fetch_strategy_history

    user_id = user["id"]
    strategy_id = req.strategy_id
    if not strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id is required.")

    strat = await db.strategies.find_one({"id": strategy_id, "user_id": user_id})
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    # Dispatch by instrument type. Option strategies MUST use the option-priced
    # engine (real CE/PE premiums incl. theta + spread cost); the underlying
    # candle engine would price them on the index and return a confidently-wrong
    # grade. Everything else (equity / futures) uses the underlying engine, where
    # the traded price IS the candle price.
    options_mode = bool(((strat.get("visual_config") or {}).get("options") or {}).get("enabled"))
    if options_mode:
        # FE-03: option strategies now use the out-of-sample bhavcopy backtester
        # (2yr real settlement prices, walk-forward verdict) instead of the retired
        # in-sample core/options_backtest.py.
        from routes.ops import ops_eod_options_backtest
        return await ops_eod_options_backtest(strategy_id=strategy_id, user=user)

    # Equity / futures: backtest on REAL underlying OHLC pulled from Upstox V3
    # (same source the live engine and /strategies/backtest use). Mock candles
    # are hard-blocked here — a grade off random-walk data is worse than no grade.
    symbol = ((strat.get("visual_config") or {}).get("symbol")
              or strat.get("symbol") or req.symbol or "").upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="Strategy has no symbol to backtest.")
    try:
        history = await _fetch_strategy_history(
            user_id, symbol, days=req.days, interval="5minute",
            allow_mock=False, strategy=strat,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    raw_candles = history.get("data") or []
    if "mock" in str(history.get("source", "")).lower():
        raise HTTPException(
            status_code=400,
            detail=f"Backtest blocked: price data source is '{history.get('source')}' (simulated). "
                   "Connect Upstox and ensure real historical data is available.",
        )
    if len(raw_candles) < 60:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient real OHLC for {symbol}: {len(raw_candles)} bars "
                   "(need >=60 for the 50-bar warmup). Connect Upstox / widen 'days'.",
        )

    engine = BacktestEngine(db)
    result = await engine.run_backtest(
        strategy_id=strategy_id,
        python_code=strat.get("python_code") or "",
        candles=raw_candles,
        strategy_metadata=strat
    )
    return result


@router.post("/core/live/activate")
async def post_core_live_activate(req: LiveActivateReq, user=Depends(get_current_user)):
    user_id = user["id"]
    if not req.acknowledge_real_money:
        raise HTTPException(status_code=400, detail="Real-money acknowledgement is required.")

    preflight = await _live_activation_preflight(user_id, req.strategy_id)
    if not preflight.get("ok"):
        raise HTTPException(status_code=400, detail={
            "message": "Live activation blocked.",
            "blockers": preflight.get("blockers") or [],
            "warnings": preflight.get("warnings") or [],
        })

    now = datetime.now(timezone.utc).isoformat()
    await db.users.update_one(
        {"id": user_id},
        {"$set": {
            "paper_mode": False,
            "allow_simulated_prices": False,
            "data_broker": "upstox",
            "execution_broker": "upstox",
            "fallback_broker": "none",
            "live_pilot_strategy_id": req.strategy_id,
            "live_pilot_started_at": now,
        }},
    )
    await db.strategies.update_many(
        {"user_id": user_id, "id": {"$ne": req.strategy_id}, "status": {"$ne": "archived"}},
        {"$set": {
            "mode": "paper",
            "mode_pinned": True,
            "live_pilot_managed": True,
            "live_pilot_excluded_at": now,
        }},
    )
    await db.strategies.update_one(
        {"user_id": user_id, "id": req.strategy_id},
        {"$set": {
            "mode": "live",
            "mode_pinned": True,
            "live_pilot_active": True,
            "live_pilot_managed": True,
            "live_pilot_activated_at": now,
            "status": "live",
            "manual_paused": False,
            "schedule_paused": False,
            "broker": "upstox",
            "requires_upstox_quality_checks": True,
            "live_readiness_required": True,
        }},
    )
    await db.live_arm_state.update_one(
        {"user_id": user_id},
        {"$set": {
            "armed": True,
            "global_live_enabled": True,
            "selected_strategy_id": req.strategy_id,
            "updated_at": now,
        }},
        upsert=True
    )
    return {"ok": True, "status": "LIVE_ARMED", **preflight}


@router.post("/core/live/arm")
async def post_core_live_arm(user=Depends(get_current_user)):
    raise HTTPException(
        status_code=400,
        detail="Use /api/core/live/activate with a selected strategy_id. Blind live arming is disabled.",
    )


@router.post("/core/live/disarm")
async def post_core_live_disarm(user=Depends(get_current_user)):
    user_id = user["id"]
    now = datetime.now(timezone.utc).isoformat()
    await db.users.update_one(
        {"id": user_id},
        {"$set": {"paper_mode": True, "allow_simulated_prices": True, "live_pilot_stopped_at": now},
         "$unset": {"live_pilot_strategy_id": "", "live_pilot_started_at": ""}},
    )
    await db.strategies.update_many(
        {"user_id": user_id, "live_pilot_managed": True},
        {"$set": {"mode": "paper", "live_pilot_stopped_at": now},
         "$unset": {
             "mode_pinned": "",
             "live_pilot_active": "",
             "live_pilot_managed": "",
             "live_pilot_activated_at": "",
             "live_pilot_excluded_at": "",
         }},
    )
    await db.live_arm_state.update_one(
        {"user_id": user_id},
        {"$set": {"armed": False, "global_live_enabled": False, "updated_at": now},
         "$unset": {"selected_strategy_id": ""}},
        upsert=True
    )
    return {"ok": True, "status": "DISARMED", "paper_mode": True}


@router.post("/core/kill-switch")
async def post_core_kill_switch(user=Depends(get_current_user)):
    await db.risk_state.update_one(
        {"_id": "global_kill_switch"},
        {"$set": {"active": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True
    )
    return {"ok": True, "status": "KILL_SWITCH_ACTIVE"}
