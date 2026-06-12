from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from core import db, get_current_user

router = APIRouter(tags=["Dashboard"])


@router.get("/risk/dashboard")
async def risk_dashboard(user=Depends(get_current_user)):
    from server import (
        get_user_settings, get_trading_day_window_ist,
        _fill_ledger_summary, _fetch_broker_positions_for_user,
        market_session_snapshot, canonical_order_status,
        ORDER_REJECTED, ORDER_CANCELLED, ORDER_SKIPPED_SIGNAL,
    )
    settings = await get_user_settings(user["id"])
    mode = "paper" if settings.get("paper_mode", True) else "live"
    start, end = get_trading_day_window_ist()
    orders = await db.orders.find(
        {"user_id": user["id"], "mode": mode, "created_at": {"$gte": start, "$lt": end}},
        {"_id": 0},
    ).to_list(1000)
    positions = await _fetch_broker_positions_for_user(user, settings)
    fill_summary = await _fill_ledger_summary(user["id"], mode=mode, start=start, end=end)
    realized = fill_summary["realized_pnl"]
    open_pnl = round(sum(float(p.get("pnl") or 0) for p in positions), 2)
    loss_limit = float(settings.get("max_daily_loss") or 0)
    gross_order_value = round(sum(abs(float(o.get("price") or 0) * int(o.get("qty") or 0)) for o in orders), 2)
    excluded_statuses = {
        ORDER_REJECTED, ORDER_CANCELLED,
        "REJECTED", "CANCELLED", "FAILED", "STALE",
        "BROKER_NOT_FOUND", "BLOCKED", "SKIPPED", ORDER_SKIPPED_SIGNAL,
    }
    trades_used = len([o for o in orders if o.get("status") not in excluded_statuses])
    max_trades = int(settings.get("max_trades_per_day") or 0)
    session = market_session_snapshot()
    return {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "mode": mode.upper(),
        "daily_loss_limit": loss_limit,
        "realized_pnl": realized,
        "realized_pnl_source": fill_summary["source"],
        "fill_count": fill_summary["fill_count"],
        "closed_trade_count": fill_summary["closed_trade_count"],
        "open_pnl": open_pnl,
        "total_pnl": round(realized + open_pnl, 2),
        "loss_remaining": round(max(0.0, loss_limit + realized), 2) if loss_limit else None,
        "trades_used": trades_used,
        "max_trades_per_day": max_trades,
        "trades_remaining": max(0, max_trades - trades_used) if max_trades else None,
        "per_strategy_capital": settings.get("per_strategy_capital"),
        "max_position_size": settings.get("max_position_size"),
        "gross_order_value": gross_order_value,
        "market_open": session["global_status"] == "OPEN",
        "market_session": session,
    }


@router.get("/trade-journal")
async def trade_journal(user=Depends(get_current_user)):
    from server import (
        _fill_ledger_summary, canonical_order_status,
        ORDER_FILLED, ORDER_CLOSED,
    )
    rows = await db.orders.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(200)
    skipped = await db.skipped_signals.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("last_seen_at", -1).to_list(200)
    fill_summary = await _fill_ledger_summary(user["id"])
    completed = [r for r in rows if canonical_order_status(r.get("status")) in {ORDER_FILLED, ORDER_CLOSED}]
    failed_actual = [r for r in rows if str(r.get("status") or "").upper() in {"FAILED", "REJECTED"}]
    wins = fill_summary["wins"]
    losses = fill_summary["losses"]
    total_pnl = fill_summary["realized_pnl"]
    return {
        "summary": {
            "orders": len(rows),
            "completed": len(completed),
            "filled_trades": fill_summary["fill_count"],
            "failed_actual_orders": len(failed_actual),
            "skipped_signals": sum(int(row.get("count") or 1) for row in skipped),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / max(1, wins + losses) * 100, 2),
            "realized_pnl": total_pnl,
            "realized_pnl_source": fill_summary["source"],
        },
        "orders": rows,
        "filled_trades": fill_summary["fills"],
        "failed_actual_orders": failed_actual,
        "skipped_signals": skipped,
    }


@router.get("/v1/dashboard/telemetry")
async def dashboard_telemetry(user=Depends(get_current_user)):
    from server import (
        option_ledger, _sync_option_ledger_strategy,
        market_session_snapshot, DEFAULT_STRATEGY_RISK,
    )
    rows = await db.strategies.find(
        {"user_id": user["id"]}, {"_id": 0, "user_id": 0},
    ).sort("created_at", -1).to_list(500)
    ledger_snapshot = option_ledger.snapshot()
    strategies_page_data = []
    for row in rows:
        ledger_row = ledger_snapshot.get(row["id"])
        if ledger_row is None:
            _sync_option_ledger_strategy(row)
            ledger_row = option_ledger.snapshot().get(row["id"], {})
        active_position = ledger_row.get("active_position")
        visual_risk = ((row.get("visual_config") or {}).get("risk") or {})
        runtime_state = ledger_row.get("state", "IDLE")
        if runtime_state in {"IDLE", "READY"} and row.get("status") == "live":
            runtime_state = "SCANNING"
        strategies_page_data.append({
            "strategy_id": row["id"],
            "name": row.get("name"),
            "status": row.get("status"),
            "asset_class": row.get("asset_class", "equity"),
            "state": runtime_state,
            "cooldown_until": ledger_row.get("cooldown_until"),
            "required_capital": ledger_row.get("required_capital", 0),
            "max_lots": 1,
            "target_pct": round(float(ledger_row.get("target_pct") or 0) * 100, 2),
            "stoploss_pct": round(float(ledger_row.get("stoploss_pct") or 0) * 100, 2),
            "trailing_sl_enabled": ledger_row.get("trailing_sl_enabled"),
            "trail_trigger_pct": round(float(ledger_row.get("trail_trigger_pct") or 0) * 100, 2),
            "trail_step_pct": round(float(ledger_row.get("trail_step_pct") or 0) * 100, 2),
            "risk_style": ledger_row.get("risk_style") or visual_risk.get("risk_style", "balanced"),
            "adaptive_exits_enabled": ledger_row.get("adaptive_exits_enabled", visual_risk.get("adaptive_exits_enabled", True)),
            "target_r_multiple": ledger_row.get("target_r_multiple", visual_risk.get("target_r_multiple", DEFAULT_STRATEGY_RISK["target_r_multiple"])),
            "cooldown_minutes": ledger_row.get("cooldown_minutes"),
            "max_trades_day": ledger_row.get("max_trades_day"),
            "risk_settings": {**(ledger_row.get("risk_settings", {}) or {}), **visual_risk},
            "time_exit_minutes": visual_risk.get("time_exit_minutes", 45),
            "indicator_exit_enabled": visual_risk.get("indicator_exit_enabled", True),
            "exit_mode": visual_risk.get("exit_mode", "tp_sl_tsl_or_signal"),
            "daily_pnl": ledger_row.get("daily_pnl", {}),
            "re_entry_allowed": ledger_row.get("state", "IDLE") == "IDLE",
            "active_position": active_position,
            "telemetry": {
                "evaluations": row.get("evaluations", 0),
                "signals_fired": row.get("signals_fired", 0),
                "last_evaluated_at": row.get("last_evaluated_at"),
                "last_signal_at": row.get("last_signal_at"),
                "last_signal_action": row.get("last_signal_action"),
                "last_signals_count": row.get("last_signals_count"),
                "last_filter_reason": row.get("last_filter_reason"),
                "last_data_source": row.get("last_data_source"),
                "last_data_live": row.get("last_data_live"),
                "last_data_reason": row.get("last_data_reason"),
                "latest_candle_age_sec": row.get("latest_candle_age_sec"),
                "last_error": row.get("last_error"),
            },
        })
    latest_ticks = option_ledger.latest_ticks(["NIFTY", "BANKNIFTY", "SENSEX"])
    session = market_session_snapshot()
    data_source = next((t["data_source"] for t in latest_ticks.values() if t.get("data_source")), None)
    simulated_active = bool(data_source and "simulated" in str(data_source).lower())
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ledger": "sqlite",
        "market_status": {
            "is_open": session["global_status"] == "OPEN",
            "session": session,
            "nifty": latest_ticks.get("NIFTY"),
            "sensex": latest_ticks.get("SENSEX"),
            "last_tick_time": max([t["tick_time"] for t in latest_ticks.values()], default=None),
            "data_source": data_source,
            "feed_source_label": "Simulated feed" if simulated_active else ("Upstox feed" if data_source else "No price feed"),
            "simulated_warning": simulated_active,
        },
        "strategies_page_data": strategies_page_data,
    }


@router.post("/risk/kill-switch")
async def risk_kill_switch(user=Depends(get_current_user)):
    from server import option_ledger
    now = datetime.now(timezone.utc).isoformat()
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "id": 1}).to_list(500)
    for row in strategies:
        option_ledger.set_kill_switch(True, strategy_id=row["id"])
    await db.users.update_one({"id": user["id"]}, {"$set": {"paper_mode": True, "ops_last_emergency_stop_at": now}})
    res = await db.strategies.update_many(
        {"user_id": user["id"], "status": "live"},
        {"$set": {"status": "paused", "last_error": f"Kill switch at {now}: automation paused and ledger entries disabled."}},
    )
    return {"ok": True, "paper_mode": True, "paused_strategies": res.modified_count, "disabled_strategies": len(strategies), "at": now}
