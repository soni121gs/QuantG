from __future__ import annotations

import uuid
import asyncio
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, validator

from core import db, get_current_user, StrategyRuntimeSettingsReq
from core.portfolio_ledger import get_strategy_pnl_today
from core.strategy_leaderboard import build_strategy_leaderboard

router = APIRouter(prefix="/strategies", tags=["Strategies"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class StrategyReq(BaseModel):
    name: str
    description: Optional[str] = ""
    kind: str
    python_code: Optional[str] = None
    visual_config: Optional[Dict[str, Any]] = None
    asset_class: Optional[str] = None
    strategy_type: Optional[str] = None
    required_capital: Optional[float] = None
    instrument_group: Optional[str] = None
    status: str = "draft"
    broker: Optional[str] = "upstox"
    mode: Optional[str] = "paper"
    market_suitability: Optional[str] = "Any Market Condition"


class StrategyOut(BaseModel):
    id: str
    name: str
    description: str
    kind: str
    python_code: Optional[str] = None
    visual_config: Optional[Dict[str, Any]] = None
    asset_class: str = "equity"
    strategy_type: str = "Option Buying"
    required_capital: float = 0.0
    instrument_group: Optional[str] = None
    ai_confidence_score: Optional[float] = None
    ai_confidence_reason: Optional[str] = None
    status: str
    created_at: str
    last_pnl: Optional[float] = None
    evaluations: Optional[int] = 0
    signals_fired: Optional[int] = 0
    last_evaluated_at: Optional[str] = None
    last_signal_at: Optional[str] = None
    last_signal_action: Optional[str] = None
    last_signals_count: Optional[int] = None
    last_data_source: Optional[str] = None
    last_data_live: Optional[bool] = None
    last_data_reason: Optional[str] = None
    last_candle_at: Optional[str] = None
    latest_candle_age_sec: Optional[float] = None
    last_error: Optional[str] = None
    broker: Optional[str] = "upstox"
    mode: Optional[str] = "paper"
    market_suitability: Optional[str] = "Any Market Condition"


class BacktestReq(BaseModel):
    strategy_id: Optional[str] = None
    python_code: Optional[str] = None
    symbol: str = "RELIANCE"
    days: int = 60
    options: Optional[Dict[str, Any]] = None
    engine: str = "local"

    @validator("days")
    def clamp_days(cls, v: int) -> int:
        return max(1, min(365, v))


class StrategyAIModifyReq(BaseModel):
    instruction: str
    apply: bool = False


class ManualOrderReq(BaseModel):
    action: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/seed-defaults")
async def seed_default_strategies(user=Depends(get_current_user)):
    from server import seed_default_strategies_for_user, migrate_user_to_v12_upstox
    inserted = await seed_default_strategies_for_user(user["id"])
    migrated = await migrate_user_to_v12_upstox(user["id"])
    return {
        "ok": True,
        "inserted": inserted,
        "migrated": migrated,
        "message": "Standardized Upstox index option presets installed. Review and backtest before enabling LIVE.",
    }


@router.get("/leaderboard")
async def strategy_leaderboard(user=Depends(get_current_user)):
    from server import _fill_ledger_summary
    user_id = user["id"]
    strategies = await db.strategies.find({"user_id": user_id}, {"_id": 0}).to_list(1000)
    strategy_ids = [s["id"] for s in strategies if s.get("id")]
    closed_trades = await db.trades.find({"user_id": user_id}, {"_id": 0}).to_list(10000)
    fill_summary = await _fill_ledger_summary(user_id)
    fill_trades = [
        {
            **row,
            "closed_at": row.get("filled_at"),
            "pnl": row.get("realized_pnl"),
            "source": "trade_fills",
        }
        for row in fill_summary["fills"]
        if float(row.get("realized_pnl") or 0) != 0
    ]
    closed_trades = [*closed_trades, *fill_trades]
    option_trades = await db.option_trade_journal.find(
        {"strategy_id": {"$in": strategy_ids}},
        {"_id": 0},
    ).to_list(10000)
    result = build_strategy_leaderboard(strategies, closed_trades, option_trades)
    result["fill_ledger"] = {
        "source": "trade_fills",
        "fill_count": fill_summary["fill_count"],
        "closed_trade_count": fill_summary["closed_trade_count"],
        "realized_pnl": fill_summary["realized_pnl"],
    }
    return result


@router.get("/live-backtest-comparison")
async def live_backtest_comparison(user=Depends(get_current_user)):
    from server import _fill_ledger_summary, canonical_order_status, ORDER_FILLED, ORDER_CLOSED
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(200)
    out = []
    for s in strategies:
        latest_backtest = await db.paper_trading_history.find_one(
            {"user_id": user["id"], "strategy_id": s["id"]},
            {"_id": 0},
            sort=[("created_at", -1)],
        )
        live_orders = await db.orders.find(
            {"user_id": user["id"], "strategy_id": s["id"]},
            {"_id": 0},
        ).sort("created_at", -1).to_list(200)
        fill_summary = await _fill_ledger_summary(user["id"], mode="live", strategy_id=s["id"])
        completed = [o for o in live_orders if canonical_order_status(o.get("status")) in {ORDER_FILLED, ORDER_CLOSED}]
        live_pnl = fill_summary["realized_pnl"]
        live_win_rate = fill_summary["win_rate"]
        backtest_pnl = float((latest_backtest or {}).get("pnl") or 0)
        drift = round(live_pnl - backtest_pnl, 2) if latest_backtest else None
        out.append({
            "strategy_id": s["id"],
            "name": s.get("name"),
            "status": s.get("status"),
            "last_data_source": s.get("last_data_source"),
            "last_data_live": s.get("last_data_live"),
            "last_signal_validation": s.get("last_signal_validation"),
            "last_filter_reason": s.get("last_filter_reason"),
            "live": {
                "orders": len(live_orders),
                "completed": len(completed),
                "fills": fill_summary["fill_count"],
                "realized_pnl": live_pnl,
                "realized_pnl_source": fill_summary["source"],
                "win_rate": live_win_rate,
            },
            "backtest": {
                "available": bool(latest_backtest),
                "pnl": round(backtest_pnl, 2),
                "win_rate": (latest_backtest or {}).get("win_rate"),
                "trades": (latest_backtest or {}).get("trades_count"),
                "created_at": (latest_backtest or {}).get("created_at"),
            },
            "drift": drift,
            "verdict": (
                "needs_backtest" if not latest_backtest else
                "live_lagging" if drift is not None and drift < -abs(backtest_pnl) * 0.25 else
                "tracking"
            ),
        })
    return {"items": out}


@router.post("/backtest")
async def backtest(req: BacktestReq, user=Depends(get_current_user)):
    from server import (
        DEFAULT_PYTHON, SYMBOLS, _fetch_strategy_history, _safe_run_python,
        _options_premium_at_exit,
    )
    import backtrader_runner
    import options_helper

    code = req.python_code
    opt_cfg = req.options or {}
    if req.strategy_id:
        row = await db.strategies.find_one({"id": req.strategy_id, "user_id": user["id"]})
        if not row:
            raise HTTPException(status_code=404, detail="Strategy not found")
        if not code:
            code = row.get("python_code")
        if not opt_cfg:
            opt_cfg = (row.get("visual_config") or {}).get("options") or {}
    if not code:
        code = DEFAULT_PYTHON

    if req.engine == "backtrader":
        try:
            options_mode = bool(opt_cfg.get("enabled"))
            target_symbol = (opt_cfg.get("underlying") or "NIFTY") if options_mode else req.symbol.upper()
            if options_mode:
                raise ValueError("Backtrader engine does not yet support options mode. Use local simulator.")
            result = backtrader_runner.run_backtest(
                symbol=target_symbol,
                python_code=code,
                starting_capital=100000,
                days=req.days,
                data_source="yfinance",
            )
            return result
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Backtrader backtest failed: {e}")

    options_mode = bool(opt_cfg.get("enabled"))
    target_symbol = (opt_cfg.get("underlying") or "NIFTY") if options_mode else req.symbol.upper()
    history = await _fetch_strategy_history(user["id"], target_symbol, days=req.days, interval="day")
    data = history["data"]
    source = history.get("source", "")

    if "mock" in str(source).lower():
        raise HTTPException(
            status_code=400,
            detail=(
                f"Backtest blocked: price data source is '{source}' (simulated). "
                "Connect Upstox and ensure real historical data is available before running a backtest."
            ),
        )

    if not data:
        raise HTTPException(status_code=400, detail=f"No price data for {target_symbol}")

    dates_seen: set = set()
    for i, candle in enumerate(data):
        d = candle.get("date", "")
        if d in dates_seen:
            raise HTTPException(status_code=400, detail=f"Backtest blocked: duplicate candle date '{d}' at index {i}.")
        dates_seen.add(d)
        h = float(candle.get("high") or 0)
        l = float(candle.get("low") or 0)
        if h < l or h <= 0 or l <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"Backtest blocked: invalid OHLC at candle {i} (date={d}): high={h}, low={l}."
            )

    signals: List[dict] = []
    try:
        signals = _safe_run_python(code, data)
    except (ValueError, Exception) as e:
        raise HTTPException(status_code=400, detail=f"Strategy error: {e}")

    starting_capital = 100000.0
    cash = starting_capital
    position = 0
    entry = 0.0
    entry_spot = 0.0
    trades: List[dict] = []
    equity_curve: List[dict] = []
    sigmap = {s["date"]: s["action"] for s in signals}

    if options_mode:
        lot_size = options_helper.LOT_SIZES.get(target_symbol.upper(), 1)
        lots = int(opt_cfg.get("lots") or 1)
        per_trade_qty = lot_size * lots
        open_option_type = None

        for d in data:
            act = sigmap.get(d["date"])
            spot = d["close"]
            if act in ("BUY", "SELL") and position == 0:
                premium = round(spot * 0.02, 2)
                open_option_type = "CE" if act == "BUY" else "PE"
                entry = premium
                entry_spot = spot
                position = per_trade_qty
                cash -= premium * per_trade_qty
                trades.append({"date": d["date"], "action": f"BUY {open_option_type}", "price": premium, "qty": per_trade_qty})
            elif act in ("BUY", "SELL") and position > 0 and open_option_type:
                exit_premium = _options_premium_at_exit(entry, spot, entry_spot, open_option_type)
                pnl = (exit_premium - entry) * position
                cash += exit_premium * position
                trades.append({"date": d["date"], "action": f"SELL {open_option_type}", "price": exit_premium, "qty": position, "pnl": round(pnl, 2)})
                position = 0
                open_option_type = None
                new_type = "CE" if act == "BUY" else "PE"
                premium = round(spot * 0.02, 2)
                open_option_type = new_type
                entry = premium
                entry_spot = spot
                position = per_trade_qty
                cash -= premium * per_trade_qty
                trades.append({"date": d["date"], "action": f"BUY {new_type}", "price": premium, "qty": per_trade_qty})
            if position > 0 and open_option_type:
                mtm_premium = _options_premium_at_exit(entry, spot, entry_spot, open_option_type)
                eq = cash + mtm_premium * position
            else:
                eq = cash
            equity_curve.append({"date": d["date"], "equity": round(eq, 2)})
    else:
        for d in data:
            act = sigmap.get(d["date"])
            if act == "BUY" and position == 0:
                position = int(cash // d["close"])
                entry = d["close"]
                cash -= position * d["close"]
                trades.append({"date": d["date"], "action": "BUY", "price": d["close"], "qty": position})
            elif act == "SELL" and position > 0:
                pnl = (d["close"] - entry) * position
                cash += position * d["close"]
                trades.append({"date": d["date"], "action": "SELL", "price": d["close"], "qty": position, "pnl": round(pnl, 2)})
                position = 0
            eq = cash + position * d["close"]
            equity_curve.append({"date": d["date"], "equity": round(eq, 2)})

    final_equity = equity_curve[-1]["equity"] if equity_curve else starting_capital
    total_pnl = round(final_equity - starting_capital, 2)
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) < 0]
    win_rate = round(len(wins) / max(1, len(wins) + len(losses)) * 100, 2)
    if req.strategy_id:
        await db.strategies.update_one({"id": req.strategy_id}, {"$set": {
            "last_pnl": total_pnl,
            "last_data_source": history.get("source"),
            "last_data_live": bool(history.get("is_live")),
        }})

    paper_trade_doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "strategy_id": req.strategy_id,
        "symbol": target_symbol,
        "mode": "options" if options_mode else "equity",
        "pnl": total_pnl,
        "trades_count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "return_pct": round(total_pnl / (starting_capital / 100), 2),
        "starting_capital": starting_capital,
        "final_equity": final_equity,
        "days_backtested": req.days,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.paper_trading_history.insert_one(paper_trade_doc)

    return {
        "engine": "local",
        "mode": "options" if options_mode else "equity",
        "symbol_analysed": target_symbol,
        "data_source": history.get("source"),
        "data_live": bool(history.get("is_live")),
        "equity_curve": equity_curve,
        "trades": trades,
        "signals": signals,
        "summary": {
            "starting_capital": starting_capital,
            "final_equity": final_equity,
            "total_pnl": total_pnl,
            "return_pct": round(total_pnl / (starting_capital / 100), 2),
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
        },
    }


@router.post("")
async def create_strategy(req: StrategyReq, user=Depends(get_current_user)):
    from server import DEFAULT_PYTHON, _strategy_out, _sync_option_ledger_strategy
    visual_config = req.visual_config or {}
    underlying = str((visual_config.get("options") or {}).get("underlying") or req.instrument_group or "").upper()
    is_mcx = (
        str(req.asset_class).lower() == "commodity"
        or str(req.instrument_group).upper() == "MCX"
        or underlying in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"}
    )
    if is_mcx:
        raise HTTPException(status_code=400, detail="MCX commodity strategies have been removed. QuantG supports Upstox NSE/BSE/NFO/BFO only.")

    risk_config = dict((visual_config.get("risk") or {}))
    if req.required_capital is not None:
        risk_config["required_capital"] = float(req.required_capital)
        visual_config = {**visual_config, "risk": risk_config}
    doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "name": req.name,
        "description": req.description or "",
        "kind": req.kind,
        "python_code": req.python_code or (DEFAULT_PYTHON if req.kind == "python" else None),
        "visual_config": visual_config,
        "asset_class": req.asset_class or ("options" if ((visual_config or {}).get("options") or {}).get("enabled") else "equity"),
        "strategy_type": req.strategy_type,
        "required_capital": req.required_capital,
        "instrument_group": req.instrument_group,
        "status": req.status,
        "broker": (req.broker or "upstox").strip().lower(),
        "mode": (req.mode or "paper").strip().lower(),
        "market_suitability": req.market_suitability or "Any Market Condition",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_pnl": None,
    }
    await db.strategies.insert_one(doc)
    _sync_option_ledger_strategy(doc)
    return _strategy_out(doc)


@router.get("")
async def list_strategies(user=Depends(get_current_user)):
    from server import _strategy_out
    rows = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    return [_strategy_out(r) for r in rows]


@router.get("/{sid}/daily-report")
async def strategy_daily_report(sid: str, user=Depends(get_current_user)):
    from server import _fetch_strategy_history
    from daily_strategy_reporter import DailyStrategyReporter
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    orders = await db.orders.find(
        {"user_id": user["id"], "source": {"$regex": f"strategy:{sid}"}},
        {"_id": 0},
    ).sort("created_at", -1).to_list(100)
    visual_config = row.get("visual_config") or {}
    options_config = visual_config.get("options") or {}
    underlying = options_config.get("underlying") or visual_config.get("symbol") or "NIFTY"
    market = await _fetch_strategy_history(user["id"], underlying, days=20, interval="day")
    candles = market.get("data") or []
    closes = [float(c.get("close", 0)) for c in candles if c.get("close") is not None]
    if len(closes) >= 2:
        change = closes[-1] - closes[0]
        trend = "BULLISH" if change > 0 else "BEARISH" if change < 0 else "NEUTRAL"
        atr = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))) / max(1, len(closes) - 1)
    else:
        trend = "NEUTRAL"
        atr = 0
    report = DailyStrategyReporter.generate_daily_report(
        strategy_id=sid,
        strategy_name=row.get("name", "Strategy"),
        underlying=underlying,
        recent_trades=orders,
        market_trend_analysis={"trend": trend, "rsi": 50, "atr": atr, "reversal_risk": 0.35},
    )
    report["data_source"] = market.get("source")
    report["pnl_today"] = await get_strategy_pnl_today(db, sid, user["id"])
    return report


@router.get("/{sid}")
async def get_strategy(sid: str, user=Depends(get_current_user)):
    from server import _strategy_out, _sync_option_ledger_strategy
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    _sync_option_ledger_strategy(row)
    return _strategy_out(row)


@router.put("/{sid}")
async def update_strategy(sid: str, req: StrategyReq, user=Depends(get_current_user)):
    from server import _strategy_out, _sync_option_ledger_strategy
    update = {k: v for k, v in req.model_dump().items() if v is not None}
    existing = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not existing:
        raise HTTPException(status_code=404, detail="Strategy not found")
    visual_config_check = update.get("visual_config") or existing.get("visual_config") or {}
    underlying_check = str((visual_config_check.get("options") or {}).get("underlying") or update.get("instrument_group") or existing.get("instrument_group") or "").upper()
    asset_class_check = str(update.get("asset_class") or existing.get("asset_class") or "").lower()
    instrument_group_check = str(update.get("instrument_group") or existing.get("instrument_group") or "").upper()
    is_mcx_check = (
        asset_class_check == "commodity"
        or instrument_group_check == "MCX"
        or underlying_check in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"}
    )
    if is_mcx_check:
        raise HTTPException(status_code=400, detail="MCX commodity strategies have been removed. QuantG supports Upstox NSE/BSE/NFO/BFO only.")
    if "asset_class" not in update and "visual_config" in update:
        update["asset_class"] = "options" if ((update["visual_config"] or {}).get("options") or {}).get("enabled") else "equity"
    if "required_capital" in update:
        visual_config = dict(update.get("visual_config") or (await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0, "visual_config": 1}) or {}).get("visual_config") or {})
        risk_config = dict(visual_config.get("risk") or {})
        risk_config["required_capital"] = float(update["required_capital"])
        visual_config["risk"] = risk_config
        update["visual_config"] = visual_config
    await db.strategies.update_one({"id": sid, "user_id": user["id"]}, {"$set": update})
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0, "user_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    _sync_option_ledger_strategy(row)
    return _strategy_out(row)


@router.post("/{sid}/ai-modify")
async def ai_modify_strategy(sid: str, req: StrategyAIModifyReq, user=Depends(get_current_user)):
    from server import (
        _google_strategy_edit_sync, _strategy_market_symbol,
        _fetch_strategy_history, _strategy_out, _sync_option_ledger_strategy,
        GEMINI_TIMEOUT_SEC,
    )
    from safe_exec import safe_run_strategy
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    instruction = (req.instruction or "").strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="Tell the AI what to change.")

    try:
        proposal = await asyncio.wait_for(
            asyncio.to_thread(_google_strategy_edit_sync, row, instruction),
            timeout=GEMINI_TIMEOUT_SEC,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"AI strategy edit failed: {e}")

    proposed_code = str(proposal.get("python_code") or "").strip()
    if "def run(data):" not in proposed_code:
        raise HTTPException(status_code=400, detail="AI proposal rejected: missing def run(data):")

    visual_config = proposal.get("visual_config") if isinstance(proposal.get("visual_config"), dict) else (row.get("visual_config") or {})
    test_row = {**row, "visual_config": visual_config}
    symbol = _strategy_market_symbol(test_row)
    history = await _fetch_strategy_history(user["id"], symbol, days=30, interval="5minute", allow_mock=True)
    data = history.get("data") or []
    if not data:
        raise HTTPException(status_code=400, detail=f"AI proposal rejected: no candles available for {symbol}")
    try:
        signals = safe_run_strategy(proposed_code, data[-250:])
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"AI proposal rejected by sandbox: {e}")

    validation = {
        "symbol": symbol,
        "candles": len(data[-250:]),
        "data_source": history.get("source"),
        "signals": len(signals),
        "last_signal": signals[-1] if signals else None,
    }
    response = {
        "ok": True,
        "applied": False,
        "proposal": {
            "name": proposal.get("name") or row.get("name"),
            "description": proposal.get("description") or row.get("description"),
            "python_code": proposed_code,
            "visual_config": visual_config,
            "notes": proposal.get("notes") if isinstance(proposal.get("notes"), list) else [],
        },
        "validation": validation,
    }
    if req.apply:
        update = {
            "name": response["proposal"]["name"],
            "description": response["proposal"]["description"],
            "python_code": proposed_code,
            "visual_config": visual_config,
            "asset_class": "options" if ((visual_config or {}).get("options") or {}).get("enabled") else row.get("asset_class", "equity"),
            "ai_modified_at": datetime.now(timezone.utc).isoformat(),
            "ai_last_instruction": instruction[:1000],
            "last_signal_validation": validation,
        }
        await db.strategies.update_one(
            {"id": sid, "user_id": user["id"]},
            {"$set": update, "$unset": {"last_error": ""}},
        )
        new_row = {**row, **update}
        _sync_option_ledger_strategy(new_row)
        response["applied"] = True
        response["strategy"] = _strategy_out({k: v for k, v in new_row.items() if k not in {"_id", "user_id"}})
    return response


@router.delete("/{sid}")
async def delete_strategy(sid: str, user=Depends(get_current_user)):
    res = await db.strategies.delete_one({"id": sid, "user_id": user["id"]})
    return {"deleted": res.deleted_count}


@router.post("/{sid}/toggle")
async def toggle_strategy(sid: str, user=Depends(get_current_user)):
    from server import option_ledger, _sync_option_ledger_strategy, get_user_settings
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    new_status = "paused" if row["status"] == "live" else "live"
    settings = await get_user_settings(user["id"])
    strategy_mode = "paper" if bool(settings.get("paper_mode", True)) else "live"
    update_fields = {
        "status": new_status,
        "broker": "upstox",
        "mode": strategy_mode,
    }
    if strategy_mode == "paper":
        update_fields.update({
            "quarantined": False,
            "halted": False,
            "is_halted": False,
            "last_filter_reason": "",
            "last_skip_reason_code": "",
            "last_error": "",
        })
    await db.strategies.update_one({"id": sid}, {"$set": update_fields})
    if new_status == "live":
        _sync_option_ledger_strategy({**row, **update_fields})
        option_ledger.set_kill_switch(False, strategy_id=sid)
    return {"status": new_status}


@router.put("/{sid}/runtime-settings")
async def update_strategy_runtime_settings(sid: str, req: StrategyRuntimeSettingsReq, user=Depends(get_current_user)):
    from server import _normalize_strategy_risk, _sync_option_ledger_strategy
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]}, {"_id": 0})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")

    if req.mode is not None and req.mode.strip().lower() == "live":
        visual_config_check = row.get("visual_config") or {}
        underlying_check = str((visual_config_check.get("options") or {}).get("underlying") or row.get("instrument_group") or "").upper()
        asset_class_check = str(row.get("asset_class") or "").lower()
        instrument_group_check = str(row.get("instrument_group") or "").upper()
        is_mcx_check = (
            asset_class_check == "commodity"
            or instrument_group_check == "MCX"
            or underlying_check in {"CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI"}
        )
        if is_mcx_check:
            raise HTTPException(status_code=400, detail="MCX commodity strategies have been removed. QuantG supports Upstox NSE/BSE/NFO/BFO only.")
    visual_config = row.get("visual_config") or {}
    risk = visual_config.get("risk") or {}
    mapping = {
        "target_pct": req.target_pct,
        "stoploss_pct": req.stoploss_pct,
        "trailing_sl_enabled": req.trailing_sl_enabled,
        "trail_trigger_pct": req.trail_trigger_pct,
        "trail_step_pct": req.trail_step_pct,
        "cooldown_minutes": req.cooldown_minutes,
        "max_trades_day": req.max_trades_day,
        "daily_loss_limit": req.daily_loss_limit,
        "required_capital": req.required_capital,
        "time_exit_minutes": req.time_exit_minutes,
        "indicator_exit_enabled": req.indicator_exit_enabled,
        "exit_mode": req.exit_mode,
        "risk_style": req.risk_style,
        "adaptive_exits_enabled": req.adaptive_exits_enabled,
        "target_r_multiple": req.target_r_multiple,
    }
    for key, value in mapping.items():
        if value is not None:
            risk[key] = value
    risk["max_lot"] = 1
    risk = _normalize_strategy_risk(risk)
    visual_config["risk"] = risk

    update_fields = {"visual_config": visual_config, "broker": "upstox"}
    if req.product is not None:
        if "options" not in visual_config:
            visual_config["options"] = {}
        visual_config["options"]["product"] = req.product
        update_fields["product"] = req.product
        row["product"] = req.product
    # Phase 2 #5: persist credit-spread structure config onto visual_config.options.
    if req.structure is not None or req.spread_width is not None or req.short_delta is not None:
        if "options" not in visual_config:
            visual_config["options"] = {}
        if req.structure is not None:
            structure = str(req.structure).strip().lower()
            if structure not in ("single_leg", "credit_spread", "debit_spread"):
                raise HTTPException(status_code=400, detail="structure must be single_leg, credit_spread, or debit_spread")
            visual_config["options"]["structure"] = structure
        if req.spread_width is not None:
            visual_config["options"]["spread_width"] = max(1, int(req.spread_width))
        if req.short_delta is not None:
            visual_config["options"]["short_delta"] = max(0.05, min(0.95, float(req.short_delta)))
    if req.broker is not None:
        update_fields["broker"] = "upstox"
        row["broker"] = "upstox"
    if req.mode is not None:
        update_fields["mode"] = req.mode.strip().lower()
        row["mode"] = req.mode.strip().lower()

    await db.strategies.update_one(
        {"id": sid, "user_id": user["id"]},
        {"$set": update_fields},
    )
    row["visual_config"] = visual_config
    _sync_option_ledger_strategy(row)
    return {"ok": True, "max_lot": 1, "risk": risk, "broker": row.get("broker"), "mode": row.get("mode")}


@router.post("/{sid}/manual-order")
async def manual_strategy_order(sid: str, req: ManualOrderReq, user=Depends(get_current_user)):
    from server import _resolve_option_for_strategy, _place_order_core
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    action = (req.action or "").upper()
    if action not in ("BUY", "SELL"):
        raise HTTPException(status_code=400, detail="action must be BUY or SELL")
    vc = row.get("visual_config") or {}
    opt_cfg = vc.get("options") or {}
    if opt_cfg.get("enabled"):
        contract = await _resolve_option_for_strategy(
            user["id"],
            row,
            underlying=opt_cfg.get("underlying", "NIFTY"),
            signal_action=action,
            strike_mode=opt_cfg.get("strike_mode", "ATM_BUY"),
            otm_points=int(opt_cfg.get("otm_points") or 0),
            expiry_offset=int(opt_cfg.get("expiry_offset") or 0),
        )
        if not contract:
            raise HTTPException(
                status_code=400,
                detail="Could not resolve Upstox option contract. Check OAuth, NFO/BFO permission, and instrument master cache.",
            )
        result = await _place_order_core(
            user_id=user["id"], symbol=opt_cfg.get("underlying", "NIFTY"),
            side=action, qty=int(opt_cfg.get("lots") or 1),
            order_type="MARKET", product=None, source=f"manual:strategy:{sid}",
            option_contract=contract,
        )
    else:
        symbol = (vc.get("symbol") or "RELIANCE").upper()
        result = await _place_order_core(
            user_id=user["id"], symbol=symbol, side=action, qty=None,
            order_type="MARKET", product=None, source=f"manual:strategy:{sid}",
        )
    await db.strategies.update_one(
        {"id": sid},
        {"$set": {"last_signal_at": datetime.now(timezone.utc).isoformat(),
                  "last_signal_action": f"MANUAL {action}"},
         "$inc": {"signals_fired": 1}},
    )
    return {"ok": True, "order": result}


@router.post("/{sid}/exit-all")
async def exit_strategy_positions(sid: str, user=Depends(get_current_user)):
    from server import _close_strategy_positions
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return await _close_strategy_positions(user["id"], sid, reason="exit")


@router.post("/{sid}/test-run")
async def test_run_strategy(sid: str, user=Depends(get_current_user)):
    from server import (
        _fetch_strategy_history, _resolve_option_for_strategy, _place_order_core,
        _validate_trade_signal, get_user_settings,
    )
    from safe_exec import safe_run_strategy
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    code = row.get("python_code") or ""
    if not code:
        raise HTTPException(status_code=400, detail="Strategy has no python code")
    vc = row.get("visual_config") or {}
    opt_cfg = vc.get("options") or {}
    options_mode = bool(opt_cfg.get("enabled"))
    if options_mode:
        symbol = (opt_cfg.get("underlying") or "NIFTY").upper()
    else:
        symbol = (vc.get("symbol") or "RELIANCE").upper()

    settings = await get_user_settings(user["id"])
    strategy_mode = row.get("mode") or ("paper" if settings.get("paper_mode", True) else "live")
    allow_mock = strategy_mode == "paper"

    history = await _fetch_strategy_history(
        user["id"], symbol, days=60, interval="5minute", allow_mock=allow_mock, strategy=row,
    )
    data: List[dict] = history["data"]
    source_label = history["source"]

    if not data:
        raise HTTPException(status_code=400, detail=f"No price data for {symbol}")

    try:
        signals = safe_run_strategy(code, data)
    except Exception as e:
        return {
            "ok": False,
            "symbol": symbol,
            "data_source": source_label,
            "candles": len(data),
            "first_candle": data[0],
            "last_candle": data[-1],
            "signals": [],
            "error": str(e),
            "order_placed": None,
        }

    order_result = None
    placed_error = None
    option_contract_used = None
    signal_validation = None
    if signals:
        last_sig = signals[-1]
        action = (last_sig.get("action") or "").upper()
        if action in ("BUY", "SELL"):
            signal_validation = _validate_trade_signal(last_sig, data, row)
            if not signal_validation.get("is_valid"):
                placed_error = (
                    f"Signal filtered: confidence {signal_validation.get('confidence')} "
                    f"< {signal_validation.get('threshold')}. "
                    f"{'; '.join(signal_validation.get('reasons') or [])}"
                )
            else:
                if not history.get("is_live", False) and not allow_mock:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Live execution blocked: Upstox candle source is not fresh ({history.get('live_reason') or source_label}). Reconnect Upstox or switch to paper mode.",
                    )
                try:
                    if options_mode:
                        option_contract_used = await _resolve_option_for_strategy(
                            user["id"], row, underlying=symbol, signal_action=action,
                            strike_mode=opt_cfg.get("strike_mode", "ATM_BUY"),
                            otm_points=int(opt_cfg.get("otm_points") or 0),
                            expiry_offset=int(opt_cfg.get("expiry_offset") or 0),
                        )
                        if not option_contract_used:
                            raise HTTPException(status_code=400, detail="Could not resolve Upstox option contract - check OAuth, exchange permissions, and instrument search.")
                        order_result = await _place_order_core(
                            user_id=user["id"], symbol=symbol, side=action,
                            qty=int(opt_cfg.get("lots") or 1),
                            order_type="MARKET", product=None,
                            source=f"test-run:strategy:{sid}",
                            option_contract=option_contract_used,
                        )
                    else:
                        order_result = await _place_order_core(
                            user_id=user["id"], symbol=symbol, side=action, qty=None,
                            order_type="MARKET", product=None,
                            source=f"test-run:strategy:{sid}",
                        )
                    await db.strategies.update_one(
                        {"id": sid},
                        {"$set": {
                            "last_signal_at": datetime.now(timezone.utc).isoformat(),
                            "last_signal_action": action,
                            "last_signals_count": len(signals),
                            "last_fired_signal_date": last_sig.get("date", ""),
                            "last_data_source": source_label,
                            "last_data_live": bool(history.get("is_live")),
                            "last_data_reason": history.get("live_reason"),
                            "last_candle_at": history.get("last_candle_at"),
                            "latest_candle_age_sec": history.get("latest_candle_age_sec"),
                        },
                         "$inc": {"signals_fired": 1, "evaluations": 1}},
                    )
                except HTTPException as e:
                    placed_error = e.detail
                except Exception as e:
                    placed_error = str(e)

    return {
        "ok": True,
        "symbol": symbol,
        "options_mode": options_mode,
        "data_source": source_label,
        "data_live": bool(history.get("is_live")),
        "candles": len(data),
        "first_candle": data[0],
        "last_candle": data[-1],
        "last_5_closes": [d.get("close") for d in data[-5:]],
        "signals": signals,
        "signal_validation": signal_validation,
        "option_contract": option_contract_used,
        "order_placed": order_result,
        "order_error": placed_error,
    }


@router.post("/{sid}/unwind")
async def unwind_strategy(sid: str, user=Depends(get_current_user)):
    from server import _close_strategy_positions
    row = await db.strategies.find_one({"id": sid, "user_id": user["id"]})
    if not row:
        raise HTTPException(status_code=404, detail="Strategy not found")
    result = await _close_strategy_positions(user["id"], sid, reason="manual-unwind")
    return {"ok": True, **result}
