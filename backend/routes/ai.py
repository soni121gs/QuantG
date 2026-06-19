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


class ActionDecisionReq(BaseModel):
    action_id: str


READ_ONLY_AGENT_TOOLS = [
    "get_execution_snapshot",
    "get_orders",
    "get_today_orders",
    "get_positions",
    "get_open_positions",
    "get_active_strategies",
    "get_upstox_status",
    "get_token_status",
    "get_market_data_status",
    "get_feed_status",
    "get_logs_errors",
    "get_risk_snapshot",
    "get_live_readiness",
    "get_today_fills",
    "get_skipped_signals",
    "get_strategy_scorecard",
    "get_daily_report",
    "get_recent_alerts",
    "search_wiki",
    "get_backtest_summary",
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


async def _run_agent_tool(name: str, user: Dict[str, Any], query: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    started = _utc_now()
    source = "mongodb"
    stale = False
    confidence = 1.0
    warnings = []
    
    try:
        if name == "get_execution_snapshot":
            from server import execution_state_manager
            data = await execution_state_manager.build_snapshot(user, sync=False)
            source = "execution_state_manager"
        elif name == "get_orders":
            data = await db.orders.find(
                {"user_id": user["id"]},
                {"_id": 0, "user_id": 0},
            ).sort("created_at", -1).to_list(15)
            source = "db.orders"
        elif name == "get_today_orders":
            from server import get_trading_day_window_ist
            start, end = get_trading_day_window_ist()
            data = await db.orders.find(
                {"user_id": user["id"], "created_at": {"$gte": start, "$lt": end}},
                {"_id": 0, "user_id": 0},
            ).sort("created_at", -1).to_list(100)
            source = "db.orders"
        elif name in ("get_positions", "get_open_positions"):
            local_positions = await db.positions.find(
                {"user_id": user["id"]},
                {"_id": 0, "user_id": 0},
            ).to_list(15)
            strategy_positions = await db.strategy_positions.find(
                {"user_id": user["id"]},
                {"_id": 0, "user_id": 0},
            ).sort("updated_at", -1).to_list(15)
            data = {
                "local_positions": local_positions,
                "strategy_positions": strategy_positions,
            }
            source = "db.positions / db.strategy_positions"
        elif name == "get_active_strategies":
            rows = await db.strategies.find(
                {"user_id": user["id"]},
                {
                    "_id": 0,
                    "user_id": 0,
                    "python_code": 0,
                },
            ).sort("created_at", -1).to_list(25)
            data = [
                row for row in rows
                if str(row.get("status") or "").lower() in {"live", "active", "running", "paused"}
            ]
            source = "db.strategies"
        elif name in ("get_upstox_status", "get_token_status"):
            from server import get_user_upstox_status
            data = await get_user_upstox_status(user["id"])
            source = "upstox_gateway"
            if not data.get("connected") or not data.get("token_valid"):
                stale = True
                confidence = 0.0
                warnings.append("Upstox API token is missing, invalid, or expired.")
        elif name in ("get_market_data_status", "get_feed_status"):
            from server import _UPSTOX_GATEWAYS, _is_nse_market_open, option_ledger
            gateway = _UPSTOX_GATEWAYS.get(user["id"])
            gateway_status = gateway.status() if gateway else {"connected": False, "last_error": "Upstox gateway not initialized"}
            latest_ticks = option_ledger.latest_ticks(["NIFTY", "BANKNIFTY", "SENSEX"])
            data = {
                "market_open": bool(_is_nse_market_open()),
                "upstox_gateway": gateway_status,
                "latest_ticks": latest_ticks,
            }
            source = "upstox_gateway / option_ledger"
            market_open = bool(data.get("market_open", False))
            feed_connected = bool(gateway_status.get("feed_running") or gateway_status.get("ws_running") or gateway_status.get("connected"))
            if not feed_connected:
                confidence = 0.0
                warnings.append("Upstox market data feed is not connected.")
            elif market_open:
                last_tick = gateway_status.get("last_tick_time") or gateway_status.get("last_tick_at")
                if not last_tick:
                    stale = True
                    confidence = 0.5
                    warnings.append("No market feed ticks received yet today.")
                else:
                    try:
                        lt = datetime.fromisoformat(str(last_tick).replace("Z", "+00:00"))
                        lt_utc = lt if lt.tzinfo else lt.replace(tzinfo=timezone.utc)
                        age = (datetime.now(timezone.utc) - lt_utc.astimezone(timezone.utc)).total_seconds()
                        if age > 180:
                            stale = True
                            confidence = 0.5
                            warnings.append(f"Market feed ticks are stale by {round(age)} seconds.")
                    except Exception:
                        pass
        elif name == "get_logs_errors":
            strategy_errors = await db.strategies.find(
                {"user_id": user["id"], "last_error": {"$nin": [None, ""]}},
                {"_id": 0, "id": 1, "name": 1, "status": 1, "last_error": 1, "last_evaluated_at": 1, "last_signal_at": 1},
            ).sort("updated_at", -1).to_list(10)
            position_errors = await db.strategy_positions.find(
                {"user_id": user["id"], "last_error": {"$nin": [None, ""]}},
                {"_id": 0, "id": 1, "strategy_id": 1, "symbol": 1, "status": 1, "last_error": 1, "updated_at": 1},
            ).sort("updated_at", -1).to_list(10)
            rejected_orders = await db.orders.find(
                {"user_id": user["id"], "status": {"$in": ["REJECTED", "rejected", "FAILED", "failed"]}},
                {"_id": 0, "user_id": 0},
            ).sort("created_at", -1).to_list(10)
            data = {
                "strategy_errors": strategy_errors,
                "position_errors": position_errors,
                "recent_rejected_orders": rejected_orders,
            }
            source = "db.strategies / db.strategy_positions / db.orders"
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
            realized = round(sum(float(o.get("realized_pnl") or 0) for o in orders), 2)
            open_pnl = round(sum(float(p.get("pnl") or 0) for p in positions), 2)
            loss_limit = float(settings.get("max_daily_loss") or 0)
            data = {
                "date": day,
                "mode": "PAPER" if settings.get("paper_mode", True) else "LIVE",
                "daily_loss_limit": loss_limit,
                "realized_pnl": realized,
                "open_pnl": open_pnl,
                "total_pnl": round(realized + open_pnl, 2),
                "loss_remaining": round(max(0.0, loss_limit + realized), 2) if loss_limit else None,
                "orders_today": len(orders),
                "max_trades_per_day": int(settings.get("max_trades_per_day") or 0),
                "per_strategy_capital": settings.get("per_strategy_capital"),
                "max_position_size": settings.get("max_position_size"),
            }
            source = "db.orders / db.positions / user_settings"
        elif name == "get_live_readiness":
            from routes.ops import ops_live_readiness
            data = await ops_live_readiness(user=user)
            source = "routes.ops.ops_live_readiness"
            if isinstance(data, dict):
                if data.get("status") == "NOT_READY":
                    confidence = 0.5
                    warnings.extend(data.get("reasons") or [])
        elif name == "get_strategy_scorecard":
            from routes.ops import ops_risk_scorecard
            data = await ops_risk_scorecard(user=user)
            source = "routes.ops.ops_risk_scorecard"
        elif name == "get_backtest_summary":
            from routes.ops import ops_options_backtest
            data = await ops_options_backtest(user=user)
            source = "routes.ops.ops_options_backtest"
        elif name == "get_today_fills":
            from server import get_trading_day_window_ist
            start, end = get_trading_day_window_ist()
            data = await db.trade_fills.find({
                "user_id": user["id"],
                "created_at": {"$gte": start, "$lt": end},
            }, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(100)
            source = "db.trade_fills"
        elif name == "get_skipped_signals":
            signals_skips = await db.signals.find({
                "user_id": user["id"],
                "status": {"$in": ["FILTERED", "REJECTED", "SKIPPED_SIGNAL", "BLOCKED", "skipped", "filtered", "rejected"]}
            }, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(50)
            agg_skips = await db.skipped_signals.find({
                "user_id": user["id"]
            }, {"_id": 0, "user_id": 0}).sort("last_seen_at", -1).to_list(50)
            data = {
                "signals_skipped": signals_skips,
                "aggregated_skipped_signals": agg_skips
            }
            source = "db.signals / db.skipped_signals"
        elif name == "search_wiki":
            import re
            match_query = {"user_id": user["id"]}
            if query:
                words = [w.strip() for w in re.split(r'\s+', query) if len(w.strip()) > 2]
                if words:
                    clauses = []
                    for word in words:
                        escaped = re.escape(word)
                        clauses.append({
                            "$or": [
                                {"title": {"$regex": escaped, "$options": "i"}},
                                {"topic": {"$regex": escaped, "$options": "i"}},
                                {"content": {"$regex": escaped, "$options": "i"}},
                                {"tags": {"$regex": escaped, "$options": "i"}},
                            ]
                        })
                    match_query["$and"] = clauses
            data = await db.wiki_docs.find(match_query, {"_id": 0, "user_id": 0}).to_list(15)
            source = "db.wiki_docs"
        elif name == "get_daily_report":
            first_strat = await db.strategies.find_one(
                {"user_id": user["id"], "status": {"$in": ["live", "active", "running"]}}
            )
            if not first_strat:
                first_strat = await db.strategies.find_one({"user_id": user["id"]})
            if first_strat:
                from routes.strategies import strategy_daily_report
                data = await strategy_daily_report(sid=first_strat["id"], user=user)
            else:
                data = {"error": "No strategies found to generate daily report."}
            source = "routes.strategies.strategy_daily_report"
        elif name == "get_recent_alerts":
            data = await db.notifications.find(
                {"user_id": user["id"]},
                {"_id": 0, "user_id": 0, "dedupe_key": 0}
            ).sort("created_at", -1).to_list(20)
            source = "db.notifications"
        else:
            raise ValueError(f"Unknown read-only tool: {name}")

        finished_time = _utc_now()
        # insert success into db.agent_tool_audit
        try:
            audit_entry = {
                "id": str(uuid.uuid4()),
                "name": name,
                "user_id": user.get("id"),
                "status": "ok",
                "timestamp": finished_time,
                "duration_ms": round((datetime.fromisoformat(finished_time) - datetime.fromisoformat(started)).total_seconds() * 1000, 2),
                "args": {
                    "query": query,
                    "kwargs": kwargs
                },
                "stale": stale,
                "confidence": confidence,
                "warnings": warnings,
                "created_at": _utc_now()
            }
            asyncio.create_task(db.agent_tool_audit.insert_one(audit_entry))
        except Exception as audit_exc:
            logger.warning("Failed to write to agent_tool_audit: %s", audit_exc)

        return {
            "name": name,
            "status": "ok",
            "source": source,
            "stale": stale,
            "confidence": float(confidence),
            "warnings": warnings,
            "user": user.get("id"),
            "account": user.get("id"),
            "timestamp": finished_time,
            "started_at": started,
            "finished_at": finished_time,
            "data": _clip_json(data),
        }
    except Exception as exc:
        finished_time = _utc_now()
        # insert error into db.agent_tool_audit
        try:
            audit_entry = {
                "id": str(uuid.uuid4()),
                "name": name,
                "user_id": user.get("id"),
                "status": "error",
                "timestamp": finished_time,
                "duration_ms": round((datetime.fromisoformat(finished_time) - datetime.fromisoformat(started)).total_seconds() * 1000, 2),
                "args": {
                    "query": query,
                    "kwargs": kwargs
                },
                "stale": True,
                "confidence": 0.0,
                "warnings": [f"Execution failed: {exc}"],
                "created_at": _utc_now()
            }
            asyncio.create_task(db.agent_tool_audit.insert_one(audit_entry))
        except Exception as audit_exc:
            logger.warning("Failed to write to agent_tool_audit on error: %s", audit_exc)

        return {
            "name": name,
            "status": "error",
            "source": source,
            "stale": True,
            "confidence": 0.0,
            "warnings": [f"Execution failed: {exc}"],
            "user": user.get("id"),
            "account": user.get("id"),
            "timestamp": finished_time,
            "started_at": started,
            "finished_at": finished_time,
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


def _parse_and_store_pending_action(reply_text: str, user_id: str) -> tuple[str, Optional[dict]]:
    import re
    pattern = r"PROPOSED_ACTION:\s*(\{.*\})"
    match = re.search(pattern, reply_text, re.DOTALL)
    if not match:
        return reply_text, None
    raw_json = match.group(1).strip()
    try:
        data = json.loads(raw_json)
        action_type = data.get("action")
        params = data.get("params", {})
        if action_type and params:
            action_id = "act_" + str(uuid.uuid4())
            cleaned_text = reply_text.replace(match.group(0), "").strip()
            return cleaned_text, {
                "action_id": action_id,
                "user_id": user_id,
                "action_type": action_type,
                "params": params,
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat()
            }
    except Exception as e:
        logger.warning("Failed to parse proposed action JSON: %s", e)
    return reply_text, None


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
You are Hermes, a trading operator and research assistant inside QuantG.

STRICT HUMAN-IN-THE-LOOP ACTION RULES (PHASE 2):
- Although you cannot directly execute database changes, you can PROPOSE professional system actions for user approval.
- Specifically, you can suggest changes to these exact fields:
  * `max_daily_loss`: Daily drawdown limit in INR (Drawdown Control).
  * `paper_mode`: Set to true (Emergency Kill Switch) or false.
  * `max_position_size`: Maximum allowed capital per single position (Position Sizing).
  * `per_strategy_capital`: Capital allocated to each strategy (Position Sizing).
  * `max_trades_per_day`: Number of allowed trades per day.
  * `default_qty`: Default order quantity.
- To propose an action, you MUST append a single block matching exactly this format at the absolute end of your response text (replacing with actual keys and values in the JSON):
PROPOSED_ACTION: {{"action": "update_profile", "params": {{"max_daily_loss": 5000.0}}}}

STRICT READ-ONLY DEFAULT RULES:
- You must never place, cancel, modify, or exit trades directly.
- Base your answers strictly on the provided read-only tool results. If the data is missing, stale, failed, or insufficient, begin with "I am unsure" and explain exactly what data is missing.
- Cite the specific tool outputs, sources, and confidence scores in your response when explaining details so the user can verify them.
- Keep the answer practical, concise, and grounded only in the tool data.
- Mention which read-only tools you used when it helps the user trust the answer.

EXAMPLES (match this grounding, tone, and the exact action format — do not copy verbatim):
Example 1 — data missing, stay honest
User: How is my BANKNIFTY position doing?
Agent: I am unsure. `get_positions` returned no BANKNIFTY position and `get_market_data_status` shows the Upstox feed disconnected, so I cannot value an open contract right now. Reconnect Upstox and ask again, or check the Positions page directly.

Example 2 — grounded answer plus a governed action proposal
User: Lower my daily loss limit to 6000 for protection.
Agent: From `get_risk_snapshot`, your daily loss limit is currently 10,000 INR with 3,200 INR realized loss today. Tightening it to 6,000 INR leaves ~2,800 INR of buffer before the kill switch trips. I can't change settings directly, so here is a proposal for you to approve.
PROPOSED_ACTION: {{"action": "update_profile", "params": {{"max_daily_loss": 6000.0}}}}

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
    # Send API key as header, NOT as a URL query param.
    # Query params appear in server/proxy access logs — using a header keeps it out of logs.
    res = requests.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
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

    # Stored chat messages keep the pending_action status captured at write time
    # ("pending"). Sync it with the live pending_actions collection so already
    # approved/rejected proposals don't reappear as actionable on reload.
    action_ids = [
        row["pending_action"]["id"]
        for row in rows
        if row.get("pending_action") and row["pending_action"].get("id")
    ]
    if action_ids:
        actions = await db.pending_actions.find(
            {"action_id": {"$in": action_ids}},
            {"_id": 0, "action_id": 1, "status": 1},
        ).to_list(len(action_ids))
        status_by_id = {a["action_id"]: a.get("status") for a in actions}
        for row in rows:
            pending = row.get("pending_action")
            if pending and pending.get("id") in status_by_id:
                pending["status"] = status_by_id[pending["id"]] or pending.get("status")

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

    tool_results = await asyncio.gather(*[_run_agent_tool(name, user, query=content) for name in READ_ONLY_AGENT_TOOLS])
    reply = await _gemini_agent_reply(content, list(tool_results), recent_messages)
    
    # Parse and store proposed action
    reply, pending_action_doc = _parse_and_store_pending_action(reply, user["id"])
    if pending_action_doc:
        await db.pending_actions.insert_one(pending_action_doc)

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
        "pending_action": {
            "id": pending_action_doc["action_id"],
            "action": pending_action_doc["action_type"],
            "params": pending_action_doc["params"],
            "status": "pending",
        } if pending_action_doc else None,
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
        "tools_used": [
            {
                "name": t["name"],
                "status": t.get("status"),
                "error": t.get("error"),
                "source": t.get("source"),
                "stale": t.get("stale"),
                "confidence": t.get("confidence"),
                "warnings": t.get("warnings"),
                "timestamp": t.get("timestamp"),
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
        "pending_action": bot_msg["pending_action"],
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
        "pending_action": bot_msg["pending_action"],
        "tools_used": bot_msg["tools_used"],
        "unavailable": unavailable,
    }


@agent_router.post("/action/approve")
async def approve_agent_action(req: ActionDecisionReq, user=Depends(get_current_user)):
    action = await db.pending_actions.find_one({"action_id": req.action_id, "user_id": user["id"]})
    if not action:
        raise HTTPException(status_code=404, detail="Pending action not found")
    if action.get("status") != "pending":
        raise HTTPException(status_code=400, detail=f"Action is already {action.get('status')}")
        
    action_type = action.get("action_type")
    params = action.get("params") or {}
    
    if action_type == "update_profile":
        update = {}
        if "paper_mode" in params:
            update["paper_mode"] = bool(params["paper_mode"])
            if update["paper_mode"] is False:
                from server import get_user_upstox_status
                upstox_status = await get_user_upstox_status(user["id"])
                if not upstox_status.get("token_valid"):
                    raise HTTPException(status_code=400, detail="Live trading disabled: Reconnect Upstox required before switching to LIVE.")
        if "default_qty" in params:
            val = int(params["default_qty"])
            if val <= 0:
                raise HTTPException(status_code=400, detail="default_qty must be > 0")
            update["default_qty"] = val
        if "max_daily_loss" in params:
            val = float(params["max_daily_loss"])
            if val < 0:
                raise HTTPException(status_code=400, detail="max_daily_loss cannot be negative")
            update["max_daily_loss"] = val
        if "max_position_size" in params:
            val = float(params["max_position_size"])
            if val < 0:
                raise HTTPException(status_code=400, detail="max_position_size cannot be negative")
            update["max_position_size"] = val
        if "per_strategy_capital" in params:
            val = float(params["per_strategy_capital"])
            if val < 0:
                raise HTTPException(status_code=400, detail="per_strategy_capital cannot be negative")
            update["per_strategy_capital"] = val
        if "max_trades_per_day" in params:
            val = int(params["max_trades_per_day"])
            if val < 0:
                raise HTTPException(status_code=400, detail="max_trades_per_day cannot be negative")
            update["max_trades_per_day"] = val
            
        if update:
            await db.users.update_one({"id": user["id"]}, {"$set": update})
            
    await db.pending_actions.update_one(
        {"action_id": req.action_id},
        {"$set": {"status": "approved", "executed_at": datetime.now(timezone.utc).isoformat()}}
    )
    return {"status": "approved", "action_id": req.action_id}


@agent_router.post("/action/reject")
async def reject_agent_action(req: ActionDecisionReq, user=Depends(get_current_user)):
    action = await db.pending_actions.find_one({"action_id": req.action_id, "user_id": user["id"]})
    if not action:
        raise HTTPException(status_code=404, detail="Pending action not found")
    if action.get("status") != "pending":
        raise HTTPException(status_code=400, detail=f"Action is already {action.get('status')}")
        
    await db.pending_actions.update_one(
        {"action_id": req.action_id},
        {"$set": {"status": "rejected", "rejected_at": datetime.now(timezone.utc).isoformat()}}
    )
    return {"status": "rejected", "action_id": req.action_id}


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
    from server import watchlist, _market_score_for_strategy, _google_ai_reply
    
    market_rows = await watchlist(user=user)
    scores = [_market_score_for_strategy(row, {r["symbol"]: r for r in market_rows}) for row in strategies]
    prompt = (
        "Analyze this QuantG market structure snapshot for educational risk context only. "
        "Mention NIFTY/BANKNIFTY/SENSEX when relevant. "
        "Keep it concise and do not promise returns.\n\n"
        + json.dumps({
            "market": market_rows[:8],
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
