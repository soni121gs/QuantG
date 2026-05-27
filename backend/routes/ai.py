from __future__ import annotations

import os
import uuid
import json
import asyncio
import requests
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import db, get_current_user

import time

_STRATEGY_SCORES_CACHE: Dict[str, Dict[str, Any]] = {}
logger = logging.getLogger(__name__)
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

router = APIRouter(prefix="/ai", tags=["AI"])
agent_router = APIRouter(prefix="/agent", tags=["AI Agent"])

class ChatReq(BaseModel):
    session_id: str = "default"
    message: str


READ_ONLY_AGENT_TOOLS = [
    "get_execution_snapshot",
    "get_orders",
    "get_positions",
    "get_active_strategies",
    "get_upstox_status",
    "get_market_data_status",
    "get_logs_errors",
    "get_risk_snapshot",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip_json(value: Any, limit: int = 24000) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= limit:
        return value
    return {
        "truncated": True,
        "limit_chars": limit,
        "preview": text[:limit],
    }


async def _run_agent_tool(name: str, user: Dict[str, Any]) -> Dict[str, Any]:
    started = _utc_now()
    try:
        if name == "get_execution_snapshot":
            from server import execution_state_manager
            data = await execution_state_manager.build_snapshot(user, sync=False)
        elif name == "get_orders":
            data = await db.orders.find(
                {"user_id": user["id"]},
                {"_id": 0, "user_id": 0},
            ).sort("created_at", -1).to_list(100)
        elif name == "get_positions":
            local_positions = await db.positions.find(
                {"user_id": user["id"]},
                {"_id": 0, "user_id": 0},
            ).to_list(100)
            strategy_positions = await db.strategy_positions.find(
                {"user_id": user["id"]},
                {"_id": 0, "user_id": 0},
            ).sort("updated_at", -1).to_list(100)
            data = {
                "local_positions": local_positions,
                "strategy_positions": strategy_positions,
            }
        elif name == "get_active_strategies":
            rows = await db.strategies.find(
                {"user_id": user["id"]},
                {
                    "_id": 0,
                    "user_id": 0,
                    "python_code": 0,
                },
            ).sort("created_at", -1).to_list(200)
            data = [
                row for row in rows
                if str(row.get("status") or "").lower() in {"live", "active", "running", "paused"}
            ]
        elif name == "get_upstox_status":
            from server import get_user_upstox_status
            data = await get_user_upstox_status(user["id"])
        elif name == "get_market_data_status":
            from server import _UPSTOX_GATEWAYS, _is_nse_market_open, _is_order_market_open, option_ledger
            gateway = _UPSTOX_GATEWAYS.get(user["id"])
            gateway_status = gateway.status() if gateway else {"connected": False, "last_error": "Upstox gateway not initialized"}
            latest_ticks = option_ledger.latest_ticks(["NIFTY", "SENSEX", "CRUDEOIL", "CRUDEOILM", "NATURALGAS"])
            data = {
                "market_open": bool(_is_nse_market_open() or _is_order_market_open("MCX")),
                "upstox_gateway": gateway_status,
                "latest_ticks": latest_ticks,
            }
        elif name == "get_logs_errors":
            strategy_errors = await db.strategies.find(
                {"user_id": user["id"], "last_error": {"$nin": [None, ""]}},
                {"_id": 0, "id": 1, "name": 1, "status": 1, "last_error": 1, "last_evaluated_at": 1, "last_signal_at": 1},
            ).sort("updated_at", -1).to_list(50)
            position_errors = await db.strategy_positions.find(
                {"user_id": user["id"], "last_error": {"$nin": [None, ""]}},
                {"_id": 0, "id": 1, "strategy_id": 1, "symbol": 1, "status": 1, "last_error": 1, "updated_at": 1},
            ).sort("updated_at", -1).to_list(50)
            rejected_orders = await db.orders.find(
                {"user_id": user["id"], "status": {"$in": ["REJECTED", "rejected", "FAILED", "failed"]}},
                {"_id": 0, "user_id": 0},
            ).sort("created_at", -1).to_list(50)
            data = {
                "strategy_errors": strategy_errors,
                "position_errors": position_errors,
                "recent_rejected_orders": rejected_orders,
            }
        elif name == "get_risk_snapshot":
            from server import get_user_settings
            settings = await get_user_settings(user["id"])
            day = datetime.now(timezone.utc).date().isoformat()
            orders = await db.orders.find(
                {"user_id": user["id"], "created_at": {"$gte": day}},
                {"_id": 0, "user_id": 0},
            ).to_list(1000)
            positions = await db.positions.find(
                {"user_id": user["id"]},
                {"_id": 0, "user_id": 0},
            ).to_list(200)
            realised = round(sum(float(o.get("realised_pnl") or 0) for o in orders), 2)
            open_pnl = round(sum(float(p.get("pnl") or 0) for p in positions), 2)
            loss_limit = float(settings.get("max_daily_loss") or 0)
            data = {
                "date": day,
                "mode": "PAPER" if settings.get("paper_mode", True) else "LIVE",
                "daily_loss_limit": loss_limit,
                "realised_pnl": realised,
                "open_pnl": open_pnl,
                "total_pnl": round(realised + open_pnl, 2),
                "loss_remaining": round(max(0.0, loss_limit + realised), 2) if loss_limit else None,
                "orders_today": len(orders),
                "max_trades_per_day": int(settings.get("max_trades_per_day") or 0),
                "per_strategy_capital": settings.get("per_strategy_capital"),
                "max_position_size": settings.get("max_position_size"),
            }
        else:
            raise ValueError(f"Unknown read-only tool: {name}")
        return {
            "name": name,
            "status": "ok",
            "started_at": started,
            "finished_at": _utc_now(),
            "data": _clip_json(data),
        }
    except Exception as exc:
        return {
            "name": name,
            "status": "error",
            "started_at": started,
            "finished_at": _utc_now(),
            "error": str(exc),
        }


def _count_rows(tool_results: List[Dict[str, Any]], tool_name: str, key: Optional[str] = None) -> int:
    tool = next((t for t in tool_results if t.get("name") == tool_name and t.get("status") == "ok"), None)
    data = tool.get("data") if tool else None
    if key and isinstance(data, dict):
        data = data.get(key)
    return len(data) if isinstance(data, list) else 0


def _local_agent_summary(tool_results: List[Dict[str, Any]]) -> str:
    upstox = next((t.get("data") for t in tool_results if t.get("name") == "get_upstox_status" and t.get("status") == "ok"), {})
    risk = next((t.get("data") for t in tool_results if t.get("name") == "get_risk_snapshot" and t.get("status") == "ok"), {})
    market = next((t.get("data") for t in tool_results if t.get("name") == "get_market_data_status" and t.get("status") == "ok"), {})
    log_data = next((t.get("data") for t in tool_results if t.get("name") == "get_logs_errors" and t.get("status") == "ok"), {})

    return "\n".join([
        "Local read-only summary:",
        f"- Upstox connected: {bool(upstox.get('connected') or upstox.get('is_connected'))}",
        f"- Market open/feed active: {bool(market.get('market_open'))}",
        f"- Active strategies checked: {_count_rows(tool_results, 'get_active_strategies')}",
        f"- Recent orders checked: {_count_rows(tool_results, 'get_orders')}",
        f"- Local positions checked: {_count_rows(tool_results, 'get_positions', 'local_positions')}",
        f"- Strategy positions checked: {_count_rows(tool_results, 'get_positions', 'strategy_positions')}",
        f"- Today PnL: {risk.get('total_pnl', 'unavailable')} ({risk.get('mode', 'mode unavailable')})",
        f"- Strategy errors: {len(log_data.get('strategy_errors') or []) if isinstance(log_data, dict) else 0}",
        f"- Rejected orders: {len(log_data.get('recent_rejected_orders') or []) if isinstance(log_data, dict) else 0}",
    ])


def _local_agent_reply(message: str, tool_results: List[Dict[str, Any]], gemini_error: Optional[str] = None) -> str:
    failed = [t for t in tool_results if t.get("status") != "ok"]
    ok_tools = [t["name"] for t in tool_results if t.get("status") == "ok"]
    if failed:
        missing = ", ".join(f"{t['name']}: {t.get('error', 'unavailable')}" for t in failed[:4])
        return (
            "I am unsure because some app data is unavailable.\n\n"
            f"Available read-only tools: {', '.join(ok_tools) or 'none'}.\n"
            f"Missing or failed data: {missing}."
        )
    if not os.environ.get("GEMINI_API_KEY"):
        return (
            "Gemini is not configured yet, so I cannot do the deeper AI interpretation.\n\n"
            f"{_local_agent_summary(tool_results)}\n\n"
            "Fix: set `GEMINI_API_KEY` in the backend environment and restart the API. "
            f"If you do not set `GEMINI_MODEL`, QuantG will use `{DEFAULT_GEMINI_MODEL}`."
        )
    detail = f"\n\nGemini error: {gemini_error[:240]}" if gemini_error else ""
    return (
        "I collected the read-only QuantG data, but Gemini did not return a usable interpretation.\n\n"
        f"{_local_agent_summary(tool_results)}\n\n"
        "Fix: check that `GEMINI_API_KEY` is valid, the backend can reach Google AI Studio, "
        f"and `GEMINI_MODEL` is a supported model such as `{DEFAULT_GEMINI_MODEL}`."
        f"{detail}"
    )


def _gemini_agent_reply_sync(message: str, tool_results: List[Dict[str, Any]], recent_messages: List[Dict[str, Any]]) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return _local_agent_reply(message, tool_results)

    model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    timeout = float(os.environ.get("GEMINI_TIMEOUT_SEC", "20"))
    history_text = "\n".join(
        f"{'User' if row.get('role') == 'user' else 'Agent'}: {str(row.get('content') or '')[:1000]}"
        for row in recent_messages[-8:]
    )
    prompt = f"""
You are Ask QuantG Agent inside QuantG.

STRICT READ-ONLY PHASE 1 RULES:
- You can only interpret the provided tool results.
- You must never place, cancel, modify, or exit trades.
- You must never change strategy, risk, broker, profile, or market-data settings.
- You must never tell the user that you performed a trading action.
- If the data is missing, stale, failed, or insufficient, begin with "I am unsure" and explain exactly what data is missing.
- Keep the answer practical, concise, and grounded only in the tool data.
- Mention which read-only tools you used when it helps the user trust the answer.

Recent conversation:
{history_text or "None"}

User question:
{message}

Read-only tool results JSON:
{json.dumps(tool_results, default=str)[:50000]}
"""
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1200,
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    res = requests.post(url, params={"key": api_key}, json=payload, timeout=timeout)
    res.raise_for_status()
    data = res.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
    return text or _local_agent_reply(message, tool_results, "empty candidate text")


async def _gemini_agent_reply(message: str, tool_results: List[Dict[str, Any]], recent_messages: List[Dict[str, Any]]) -> str:
    try:
        timeout = float(os.environ.get("GEMINI_TIMEOUT_SEC", "20"))
        return await asyncio.wait_for(
            asyncio.to_thread(_gemini_agent_reply_sync, message, tool_results, recent_messages),
            timeout=timeout + 2,
        )
    except Exception as exc:
        logger.warning("Gemini read-only agent reply failed: %s", exc)
        return _local_agent_reply(message, tool_results, str(exc))


@router.get("/chat/{session_id}")
async def get_ai_chat(session_id: str, user=Depends(get_current_user)):
    rows = await db.ai_chats.find(
        {"user_id": user["id"], "session_id": session_id},
        {"_id": 0, "user_id": 0, "session_id": 0},
    ).sort("created_at", 1).to_list(100)
    return rows


@router.get("/status")
async def ai_status(user=Depends(get_current_user)):
    configured = bool(os.environ.get("GEMINI_API_KEY"))
    gemini_model = os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
    return {
        "provider": "google-ai-studio-rest" if configured else "local-fallback",
        "model": gemini_model if configured else "quantg-local-rules",
        "gemini_configured": configured,
        "google_genai_sdk_available": False,
        "sdk_error": None,
        "transport": "rest",
        "setup_hint": None if configured else "Set GEMINI_API_KEY in the backend environment and restart the API.",
    }


@router.post("/chat")
async def ai_chat(req: ChatReq, user=Depends(get_current_user)):
    content = req.message.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message is required")

    user_msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user["id"],
        "session_id": req.session_id,
    }
    recent_messages = await db.ai_chats.find(
        {"user_id": user["id"], "session_id": req.session_id},
        {"_id": 0, "role": 1, "content": 1},
    ).sort("created_at", -1).to_list(8)
    recent_messages = list(reversed(recent_messages))
    
    # Runtime dynamic imports to avoid circular dependencies
    from server import _google_ai_reply, _quantbot_reply, GEMINI_MODEL
    
    provider = "google-ai-studio" if os.environ.get("GEMINI_API_KEY") else "local-fallback"
    reply = await _google_ai_reply(content, recent_messages)
    if provider == "google-ai-studio" and reply == _quantbot_reply(content):
        provider = "local-fallback"
        
    bot_msg = {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": reply,
        "provider": provider,
        "model": GEMINI_MODEL if provider == "google-ai-studio" else "quantg-local-rules",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user["id"],
        "session_id": req.session_id,
    }
    await db.ai_chats.insert_many([user_msg, bot_msg])
    return {k: v for k, v in bot_msg.items() if k not in {"_id", "user_id", "session_id"}}


@agent_router.post("/chat")
async def agent_chat(req: ChatReq, user=Depends(get_current_user)):
    content = req.message.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message is required")

    now = _utc_now()
    user_msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": content,
        "created_at": now,
        "user_id": user["id"],
        "session_id": req.session_id,
        "surface": "ask-quantg-agent",
    }
    recent_messages = await db.ai_chats.find(
        {"user_id": user["id"], "session_id": req.session_id},
        {"_id": 0, "role": 1, "content": 1},
    ).sort("created_at", -1).to_list(8)
    recent_messages = list(reversed(recent_messages))

    tool_results = await asyncio.gather(*[_run_agent_tool(name, user) for name in READ_ONLY_AGENT_TOOLS])
    reply = await _gemini_agent_reply(content, list(tool_results), recent_messages)
    provider = "google-ai-studio" if os.environ.get("GEMINI_API_KEY") else "local-fallback"
    failed_tools = [t for t in tool_results if t.get("status") != "ok"]
    unavailable = [
        {"tool": t["name"], "error": t.get("error", "unavailable")}
        for t in failed_tools
    ]

    bot_msg = {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": reply,
        "provider": provider,
        "model": os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL) if provider == "google-ai-studio" else "quantg-local-rules",
        "created_at": _utc_now(),
        "user_id": user["id"],
        "session_id": req.session_id,
        "surface": "ask-quantg-agent",
        "read_only": True,
        "tool_calls": [
            {
                "name": t["name"],
                "status": t.get("status"),
                "started_at": t.get("started_at"),
                "finished_at": t.get("finished_at"),
                "error": t.get("error"),
            }
            for t in tool_results
        ],
    }
    audit_doc = {
        "id": str(uuid.uuid4()),
        "user_id": user["id"],
        "session_id": req.session_id,
        "question": content,
        "gemini_response": reply,
        "provider": provider,
        "model": bot_msg["model"],
        "read_only": True,
        "rules": {
            "can_place_orders": False,
            "can_cancel_orders": False,
            "can_modify_trades": False,
            "can_change_strategy_risk_broker_settings": False,
        },
        "tool_calls": list(tool_results),
        "created_at": _utc_now(),
    }
    await db.ai_chats.insert_many([user_msg, bot_msg])
    await db.agent_audit_logs.insert_one(audit_doc)
    return {
        "id": bot_msg["id"],
        "role": "assistant",
        "content": reply,
        "provider": bot_msg["provider"],
        "model": bot_msg["model"],
        "created_at": bot_msg["created_at"],
        "read_only": True,
        "tools_used": [
            {
                "name": t["name"],
                "status": t.get("status"),
                "error": t.get("error"),
            }
            for t in tool_results
        ],
        "unavailable": unavailable,
    }


@router.get("/strategy-scores")
async def ai_strategy_scores(user=Depends(get_current_user)):
    user_id = user["id"]
    now = time.monotonic()
    cached = _STRATEGY_SCORES_CACHE.get(user_id)
    if cached and now - cached["timestamp"] < 30.0:
        return cached["data"]

    rows = await db.strategies.find({"user_id": user_id}, {"_id": 0, "user_id": 0}).to_list(500)
    
    # Runtime dynamic imports to avoid circular dependencies
    from server import watchlist, commodity_watchlist, _market_score_for_strategy
    
    market_rows = await watchlist(user=user)
    commodity_rows = await commodity_watchlist(user=user)
    market_by_symbol = {r["symbol"]: r for r in [*market_rows, *commodity_rows]}
    scores = [_market_score_for_strategy(row, market_by_symbol) for row in rows]
    for score in scores:
        score["user_id"] = user_id
        await db.strategy_ai_scores.update_one(
            {"strategy_id": score["strategy_id"], "user_id": user_id},
            {"$set": score},
            upsert=True,
        )
    result = {
        "scores": [{k: v for k, v in score.items() if k != "user_id"} for score in scores],
        "provider": "gemini-context" if os.environ.get("GEMINI_API_KEY") else "local-market-structure",
    }
    _STRATEGY_SCORES_CACHE[user_id] = {
        "timestamp": now,
        "data": result,
    }
    return result


@router.get("/market-analysis")
async def ai_market_analysis(user=Depends(get_current_user)):
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).to_list(50)
    
    # Runtime dynamic imports to avoid circular dependencies
    from server import watchlist, commodity_watchlist, _market_score_for_strategy, _google_ai_reply
    
    market_rows = await watchlist(user=user)
    commodity_rows = await commodity_watchlist(user=user)
    scores = [_market_score_for_strategy(row, {r["symbol"]: r for r in [*market_rows, *commodity_rows]}) for row in strategies]
    prompt = (
        "Analyze this QuantG market structure snapshot for educational risk context only. "
        "Mention NIFTY/SENSEX and MCX crude oil/natural gas when relevant. "
        "Keep it concise and do not promise returns.\n\n"
        + json.dumps({
            "market": market_rows[:8],
            "commodities": commodity_rows,
            "strategy_scores": scores[:12],
        }, default=str)[:9000]
    )
    return {
        "provider": "google-ai-studio" if os.environ.get("GEMINI_API_KEY") else "local-fallback",
        "content": await _google_ai_reply(prompt, []),
        "scores": scores,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/training-context")
async def ai_training_context(user=Depends(get_current_user)):
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).to_list(200)
    recent_scores = await db.strategy_ai_scores.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("generated_at", -1).to_list(100)
    
    # Runtime dynamic imports to avoid circular dependencies
    from server import watchlist, commodity_watchlist, _strategy_out
    
    return {
        "purpose": "Context-feed payload for Gemini prompts and offline fine-tuning experiments.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market": await watchlist(user=user),
        "commodities": await commodity_watchlist(user=user),
        "strategies": [_strategy_out(row).model_dump() for row in strategies],
        "recent_ai_scores": recent_scores,
    }
