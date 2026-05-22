from __future__ import annotations

import os
import uuid
import json
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import db, get_current_user

router = APIRouter(prefix="/ai", tags=["AI"])

class ChatReq(BaseModel):
    session_id: str = "default"
    message: str


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
    gemini_model = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash"
    return {
        "provider": "google-ai-studio-rest" if configured else "local-fallback",
        "model": gemini_model if configured else "quantg-local-rules",
        "gemini_configured": configured,
        "google_genai_sdk_available": False,
        "sdk_error": None,
        "transport": "rest",
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


@router.get("/strategy-scores")
async def ai_strategy_scores(user=Depends(get_current_user)):
    rows = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).to_list(500)
    
    # Runtime dynamic imports to avoid circular dependencies
    from server import watchlist, commodity_watchlist, _market_score_for_strategy
    
    market_rows = await watchlist(user=user)
    commodity_rows = await commodity_watchlist(user=user)
    market_by_symbol = {r["symbol"]: r for r in [*market_rows, *commodity_rows]}
    scores = [_market_score_for_strategy(row, market_by_symbol) for row in rows]
    for score in scores:
        score["user_id"] = user["id"]
        await db.strategy_ai_scores.update_one(
            {"strategy_id": score["strategy_id"], "user_id": user["id"]},
            {"$set": score},
            upsert=True,
        )
    return {
        "scores": [{k: v for k, v in score.items() if k != "user_id"} for score in scores],
        "provider": "gemini-context" if os.environ.get("GEMINI_API_KEY") else "local-market-structure",
    }


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
