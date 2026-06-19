from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, Depends

from core import db, get_current_user

router = APIRouter(tags=["Readiness"])


@router.get("/strategy-readiness")
async def strategy_readiness(user=Depends(get_current_user)):
    from server import _build_strategy_readiness_rows

    rows = await _build_strategy_readiness_rows(user["id"])
    summary = {
        "ready": sum(1 for r in rows if r["status"] == "READY"),
        "warning": sum(1 for r in rows if r["status"] == "WARNING"),
        "blocked": sum(1 for r in rows if r["status"] == "BLOCKED"),
        "quarantined": sum(1 for r in rows if r["status"] == "QUARANTINED"),
    }
    return {"status": "READY" if summary["blocked"] == 0 and summary["quarantined"] == 0 else "WARNING", "summary": summary, "strategies": rows}


@router.get("/paper-readiness")
async def paper_readiness(user=Depends(get_current_user)):
    from server import (
        ORDER_PAPER_CREATED,
        ORDER_PAPER_FILLED,
        ORDER_SKIPPED_SIGNAL,
        _build_strategy_readiness_rows,
        _is_fake_34_price,
        _order_has_real_upstox_price_metadata,
        get_trading_day_window_ist,
        get_user_settings,
        get_user_upstox_status,
    )

    user_id = user["id"]
    settings = await get_user_settings(user_id)
    start, end = get_trading_day_window_ist()
    upstox = await get_user_upstox_status(user_id)
    strategy_rows = await _build_strategy_readiness_rows(user_id)
    active_strategy_count = sum(1 for r in strategy_rows if r.get("runtime_status") == "live" and r.get("mode") == "paper")
    quarantined_count = sum(1 for r in strategy_rows if r["status"] == "QUARANTINED")
    skipped_count = await db.signals.count_documents({"user_id": user_id, "processed_at": {"$gte": start, "$lt": end}, "status": {"$in": ["FILTERED", "REJECTED", "SKIPPED_SIGNAL", "BLOCKED"]}})
    valid_paper_order_count = await db.orders.count_documents({"user_id": user_id, "mode": "paper", "created_at": {"$gte": start, "$lt": end}, "status": {"$nin": [ORDER_SKIPPED_SIGNAL, "SKIPPED", "FAILED", "REJECTED"]}})
    recent_orders = await db.orders.find({"user_id": user_id, "mode": "paper", "created_at": {"$gte": start, "$lt": end}}, {"_id": 0}).sort("created_at", -1).limit(100).to_list(100)
    fake_or_unproven = [o for o in recent_orders if _is_fake_34_price(o.get("price")) and not _order_has_real_upstox_price_metadata(o)]
    missing_price_source = [o for o in recent_orders if str(o.get("status") or "").upper() in {ORDER_PAPER_CREATED, ORDER_PAPER_FILLED} and not o.get("price_source")]
    latest_skips = await db.signals.find({"user_id": user_id, "processed_at": {"$gte": start, "$lt": end}, "rejection_reason": {"$ne": None}}, {"_id": 0, "id": 1, "strategy_id": 1, "target_symbol": 1, "status": 1, "rejection_reason": 1, "processed_at": 1}).sort("processed_at", -1).limit(10).to_list(10)
    feed = (upstox.get("gateway") or {})
    blockers = []
    if not settings.get("paper_mode", True):
        blockers.append("paper_mode disabled")
    if fake_or_unproven:
        blockers.append("unproven 34.xx paper orders found today")
    if missing_price_source:
        blockers.append("paper orders missing price_source metadata")
    status = "BLOCKED" if blockers else ("WARNING" if quarantined_count or skipped_count else "READY")
    return {
        "status": status,
        "blockers": blockers,
        "paper_mode": bool(settings.get("paper_mode", True)),
        "live_trading_disabled": bool(settings.get("paper_mode", True)),
        "allow_simulated_prices": bool(settings.get("allow_simulated_prices")),
        "upstox": {
            "connected": bool(upstox.get("connected")),
            "token_valid": bool(upstox.get("token_valid") or upstox.get("authenticated")),
            "last_tick_at": feed.get("last_tick_at"),
            "ticks": feed.get("ticks", 0),
            "feed_stalled": bool(upstox.get("feed_stalled")),
            "feed_stalled_reason": upstox.get("feed_stalled_reason"),
        },
        "feed_status": "READY" if feed.get("last_tick_at") else "WARNING",
        "active_strategy_count": active_strategy_count,
        "quarantined_strategy_count": quarantined_count,
        "skipped_signal_count": skipped_count,
        "valid_paper_order_count": valid_paper_order_count,
        "fake_suspicious_price_blocked_count": len(fake_or_unproven),
        "paper_orders_missing_price_source": len(missing_price_source),
        "latest_skipped_reasons": latest_skips,
        "latest_order_source_validation": [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "price": o.get("price"),
                "price_source": o.get("price_source"),
                "price_received_at": o.get("price_received_at"),
                "real_upstox_metadata": _order_has_real_upstox_price_metadata(o),
            }
            for o in recent_orders[:10]
        ],
    }


@router.get("/live/readiness")
async def live_readiness(user=Depends(get_current_user)):
    from server import get_user_settings, get_user_upstox_status

    checks = []
    settings = await get_user_settings(user["id"])
    upstox_status = await get_user_upstox_status(user["id"])
    required_keys_ok = bool(upstox_status.get("keys_saved"))
    checks.append({
        "id": "broker_keys",
        "label": "Upstox credentials saved",
        "ok": required_keys_ok,
        "hint": "Save Upstox credentials on Broker Keys" if not required_keys_ok else None,
    })
    required_sessions_ok = bool(upstox_status.get("connected") and upstox_status.get("token_valid"))
    checks.append({
        "id": "upstox_session",
        "label": "Active Upstox session",
        "ok": required_sessions_ok,
        "detail": "data=upstox, execution=upstox",
        "hint": "Reconnect Upstox required on Broker Keys" if not required_sessions_ok else None,
    })
    checks.append({
        "id": "funds",
        "label": "Sufficient funds in account",
        "ok": bool(upstox_status.get("connected")),
        "detail": "Upstox connected; live margin check runs from the Upstox account.",
        "hint": "Connect Upstox before LIVE" if not upstox_status.get("connected") else None,
    })
    checks.append({
        "id": "risk_limits",
        "label": "Risk limits configured",
        "ok": settings.get("max_position_size", 0) > 0 and settings.get("max_daily_loss", 0) > 0,
        "detail": f"Max position Rs {settings['max_position_size']:.0f} / Daily loss cap Rs {settings['max_daily_loss']:.0f}",
        "hint": "Configure on Profile" if (settings.get("max_position_size", 0) <= 0 or settings.get("max_daily_loss", 0) <= 0) else None,
    })
    ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    is_weekday = ist_now.weekday() < 5
    minutes_now = ist_now.hour * 60 + ist_now.minute
    nse_open = is_weekday and (9 * 60 + 15) <= minutes_now <= (15 * 60 + 30)
    checks.append({
        "id": "market_hours",
        "label": "NSE/BSE market open",
        "ok": nse_open,
        "detail": ist_now.strftime("%a %H:%M IST"),
        "hint": "Market trades 09:15 - 15:30 IST, Mon-Fri" if not nse_open else None,
    })
    gateway_status = upstox_status.get("gateway") or {}
    feed_status = gateway_status.get("feed_status") or upstox_status.get("feed_status") or {}
    selected_tick_ok = bool(upstox_status.get("connected") and (feed_status.get("connected") or gateway_status.get("ws_running")))
    checks.append({
        "id": "tick_feed",
        "label": "Upstox realtime tick feed",
        "ok": selected_tick_ok,
        "detail": f"upstox feed {feed_status.get('state') or 'running'}" if selected_tick_ok else f"feed not running: {upstox_status.get('reason') or feed_status.get('last_error') or 'not running'}",
        "hint": "Reconnect Upstox on Broker Keys, then restart the Upstox feed." if not selected_tick_ok else None,
    })
    paper_mode = bool(settings.get("paper_mode", True))
    overall_ready = all(c["ok"] for c in checks if c["id"] != "market_hours")
    return {
        "ready": overall_ready,
        "market_open": nse_open,
        "current_mode": "PAPER" if paper_mode else "LIVE",
        "broker": "upstox",
        "supported_segments": ["NSE_EQ", "BSE_EQ", "NSE_FO", "BSE_FO"],
        "removed_segments": ["MCX_FO"],
        "checks": checks,
    }


@router.get("/core/health")
async def get_core_health():
    return {
        "status": "healthy",
        "engine": "core_unified",
        "shadow_mode": os.environ.get("CORE_ENGINE_SHADOW_MODE", "true") == "true",
        "version": "1.0.0"
    }


@router.get("/core/market-status")
async def get_core_market_status():
    from core.market_clock import get_market_clock_snapshot

    return get_market_clock_snapshot()


@router.get("/core/feed-status")
async def get_core_feed_status(user=Depends(get_current_user)):
    from server import get_user_upstox_status

    user_id = user["id"]
    status = await get_user_upstox_status(user_id)
    return {
        "connected": status.get("connected", False),
        "token_valid": status.get("token_valid", False),
        "feed_stalled": status.get("feed_stalled", False),
        "feed_stalled_reason": status.get("feed_stalled_reason"),
        "feed_source": "upstox_websocket_v3",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@router.get("/trading/live-readiness")
async def get_trading_live_readiness(user=Depends(get_current_user)):
    from server import (
        _maybe_await,
        broker_reconciliation_summary,
        get_user_upstox_gateway,
        get_user_upstox_status,
    )

    user_id = user["id"]
    checks: list[dict[str, Any]] = []

    live_env_enabled = os.environ.get("CORE_ENGINE_LIVE_ENABLED", "false").lower() == "true"
    checks.append({
        "id": "live_env_enabled",
        "label": "CORE_ENGINE_LIVE_ENABLED set to true in .env",
        "ok": live_env_enabled,
        "detail": f"CORE_ENGINE_LIVE_ENABLED={os.environ.get('CORE_ENGINE_LIVE_ENABLED')}",
        "hint": "Set CORE_ENGINE_LIVE_ENABLED=true in .env to allow live trading."
    })

    arm_state = await db.live_arm_state.find_one({"user_id": user_id})
    live_db_armed = bool(arm_state and arm_state.get("armed"))
    global_live_enabled = bool(arm_state and arm_state.get("global_live_enabled"))
    checks.append({
        "id": "live_db_armed",
        "label": "System manually armed in database",
        "ok": live_db_armed and global_live_enabled,
        "detail": f"armed={live_db_armed}, global_live_enabled={global_live_enabled}",
        "hint": "Arm system in Control Room or call /api/core/live/arm"
    })

    upstox_status = await get_user_upstox_status(user_id)
    keys_saved = bool(upstox_status.get("keys_saved"))
    checks.append({
        "id": "upstox_keys",
        "label": "Upstox key/secret configured",
        "ok": keys_saved,
        "detail": f"keys_saved={keys_saved}",
        "hint": "Save Upstox API keys in Broker Keys page."
    })

    token_valid = bool(upstox_status.get("connected") and upstox_status.get("token_valid"))
    checks.append({
        "id": "upstox_token",
        "label": "Upstox OAuth session active",
        "ok": token_valid,
        "detail": f"connected={upstox_status.get('connected')}, token_valid={upstox_status.get('token_valid')}",
        "hint": "Log in to Upstox through Broker Keys callback."
    })

    gateway_status = upstox_status.get("gateway") or {}
    feed_status = gateway_status.get("feed_status") or upstox_status.get("feed_status") or {}
    feed_ok = bool(upstox_status.get("connected") and (feed_status.get("connected") or gateway_status.get("ws_running")))
    checks.append({
        "id": "feed_status",
        "label": "Upstox live price feed active",
        "ok": feed_ok,
        "detail": f"state={feed_status.get('state') or 'running'}",
        "hint": "Restart Upstox feed from Control Room."
    })

    sync_meta = await _maybe_await(db.upstox_instrument_sync_meta.find_one({"_id": "daily-json"}, {"_id": 0})) or {}
    instrument_count = await _maybe_await(db.upstox_instruments.count_documents({}))
    if not isinstance(instrument_count, (int, float)):
        instrument_count = 0
    instrument_sync_ok = bool(sync_meta.get("completed_at") and instrument_count > 0)
    checks.append({
        "id": "upstox_instrument_master",
        "label": "Daily Upstox instrument master synced",
        "ok": instrument_sync_ok,
        "detail": f"completed_at={sync_meta.get('completed_at')}, instruments={instrument_count}",
        "hint": "Run /api/upstox/instruments/sync before live order placement."
    })

    try:
        reconciliation_gateway = await get_user_upstox_gateway(user_id) if token_valid else None
        reconciliation = await broker_reconciliation_summary(db, user_id, reconciliation_gateway)
    except Exception as exc:
        reconciliation = {"status": "UNKNOWN", "errors": [str(exc)[:200]], "pending_orders": []}
    recon_ok = str(reconciliation.get("status") or "").upper() in {"OK", "READY", "NO_GATEWAY"} and not reconciliation.get("errors")
    checks.append({
        "id": "broker_truth_reconciliation",
        "label": "Broker truth reconciliation clean",
        "ok": recon_ok,
        "detail": f"status={reconciliation.get('status')}, pending={len(reconciliation.get('pending_orders') or [])}",
        "hint": "Resolve pending/unknown broker orders before enabling live mode."
    })

    exchange_rules = await db.system_config.find_one({"_id": "exchange_rules"})
    rules_ok = bool(exchange_rules and exchange_rules.get("lot_sizes"))
    checks.append({
        "id": "exchange_rules",
        "label": "Instrument lot sizes and exchange rules resolution",
        "ok": rules_ok,
        "detail": f"rules_present={rules_ok}",
        "hint": "Run MCX resolver initialization or verify MongoDB system_config."
    })

    funds = await db.funds.find_one({"user_id": user_id})
    funds_ok = bool(funds and float(funds.get("available_margin") or 0) > 0)
    margin_value = funds.get("available_margin") or 0 if funds else 0
    checks.append({
        "id": "funds_presence",
        "label": "Funds and margins balance present",
        "ok": funds_ok,
        "detail": f"margin=\u20b9{margin_value:.2f}",
        "hint": "Fetch funds from Upstox broker API or check fund ledger."
    })

    kill_switch = await db.risk_state.find_one({"_id": "global_kill_switch"})
    kill_active = bool(kill_switch and kill_switch.get("active"))
    checks.append({
        "id": "kill_switch",
        "label": "Global kill-switch status (INACTIVE is ok)",
        "ok": not kill_active,
        "detail": f"kill_switch_active={kill_active}",
        "hint": "Deactivate global kill-switch or reset risk state in DB."
    })

    unknown_orders_count = await db.orders.count_documents({
        "user_id": user_id,
        "status": "UNKNOWN_NEEDS_REVIEW"
    })
    checks.append({
        "id": "stale_reconciliation",
        "label": "No active orders needing manual review",
        "ok": unknown_orders_count == 0,
        "detail": f"orders_needing_review={unknown_orders_count}",
        "hint": "Manually reconcile orders marked UNKNOWN_NEEDS_REVIEW."
    })

    overall_ok = all(c["ok"] for c in checks)
    return {
        "ok": overall_ok,
        "live_order_placement_ready": overall_ok,
        "live_auto_trading_enabled": False,
        "live_auto_trading_default": "disabled",
        "broker": "upstox",
        "checks": checks,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


@router.get("/core/live/readiness")
async def get_core_live_readiness(user=Depends(get_current_user)):
    from core.live_safety_firewall import LiveSafetyFirewall

    user_id = user["id"]
    firewall = LiveSafetyFirewall(db)
    res = await firewall.verify_readiness(user_id, "manual", "NIFTY")
    return res
