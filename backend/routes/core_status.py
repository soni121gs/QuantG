from __future__ import annotations

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


@router.get("/core/strategies")
async def get_core_strategies(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.strategies.find({"user_id": user_id}).to_list(length=200)


@router.get("/core/orders")
async def get_core_orders(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.orders.find({"user_id": user_id}).sort("created_at", -1).to_list(length=200)


@router.get("/core/positions")
async def get_core_positions(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.strategy_positions.find({"user_id": user_id}).to_list(length=200)


@router.get("/core/performance")
async def get_core_performance(user=Depends(get_current_user)):
    user_id = user["id"]
    tracker = PerformanceTracker(db)
    return await tracker.rebuild_leaderboard(user_id)


@router.get("/core/backtests")
async def get_core_backtests(user=Depends(get_current_user)):
    user_id = user["id"]
    return await db.backtest_runs.find().sort("created_at", -1).to_list(length=100)


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
        from core.options_backtest import OptionsBacktestEngine
        engine = OptionsBacktestEngine(db)
        return await engine.run(strat)

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


@router.post("/core/live/arm")
async def post_core_live_arm(user=Depends(get_current_user)):
    user_id = user["id"]
    await db.live_arm_state.update_one(
        {"user_id": user_id},
        {"$set": {"armed": True, "global_live_enabled": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True
    )
    return {"ok": True, "status": "ARMED"}


@router.post("/core/live/disarm")
async def post_core_live_disarm(user=Depends(get_current_user)):
    user_id = user["id"]
    await db.live_arm_state.update_one(
        {"user_id": user_id},
        {"$set": {"armed": False, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True
    )
    return {"ok": True, "status": "DISARMED"}


@router.post("/core/kill-switch")
async def post_core_kill_switch(user=Depends(get_current_user)):
    await db.risk_state.update_one(
        {"_id": "global_kill_switch"},
        {"$set": {"active": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
        upsert=True
    )
    return {"ok": True, "status": "KILL_SWITCH_ACTIVE"}
