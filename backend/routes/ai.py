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


@agent_router.get("/skills")
async def list_agent_skills(user=Depends(get_current_user)):
    """Expose the active Hermes operator skill pack playbooks."""
    from core.skills import HERMES_SKILL_PACK
    return list(HERMES_SKILL_PACK.values())


@agent_router.get("/actions/pending")
async def get_pending_actions(user=Depends(get_current_user)):
    """Retrieve all pending operator actions for the current user."""
    rows = await db.pending_actions.find(
        {"user_id": user["id"], "status": "pending"},
        {"_id": 0}
    ).sort("created_at", -1).to_list(100)
    return rows

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
    "get_knowledge_graph",
    "get_backtest_summary",
    "get_core_events",
    "get_agent_tool_audit",
    "get_strategy_score_explained",
    "get_historical_context",
    "recall_memory",
    "get_external_context",
    "get_trade_attribution",
    "get_hermes_brain_health",
    "get_hermes_oos_validation",
    "get_research_hypotheses",
    "get_research_critique",
    "get_research_desk",
    "get_edge_lab_snapshot",
    "get_intraday_oos",
    "get_regime_status",
    "get_edge_math_advice",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gemini_grounded_search_sync(query: str) -> Dict[str, Any]:
    """HSB-06: Pull external macro/news context via Gemini's Google-Search grounding.

    Returns a dict tagged EXTERNAL/UNVERIFIED with source URLs. This is the ONLY tool
    that reaches outside QuantG's own data — by design law its output must never enter a
    numeric trading claim, only macro/event context (e.g. 'is there event risk today').
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"error": "GEMINI_API_KEY not configured — external context unavailable."}

    model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    timeout = float(os.environ.get("GEMINI_TIMEOUT_SEC", "20"))
    payload = {
        "contents": [{"parts": [{"text": (
            "Provide brief, factual market/macro context for an Indian (NSE/BSE) options "
            "trader for this query. Focus on scheduled events, news, and event-risk only. "
            "Do NOT give trading advice or price targets.\n\nQuery: " + query
        )}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 600},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    res = requests.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    res.raise_for_status()
    data = res.json()
    candidate = (data.get("candidates") or [{}])[0]
    parts = candidate.get("content", {}).get("parts", [])
    text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()

    sources = []
    grounding = candidate.get("groundingMetadata", {}) or {}
    for chunk in (grounding.get("groundingChunks") or []):
        web = chunk.get("web") or {}
        if web.get("uri"):
            sources.append({"title": web.get("title"), "uri": web.get("uri")})

    return {
        "tag": "EXTERNAL/UNVERIFIED",
        "query": query,
        "summary": text or "(no external context returned)",
        "sources": sources,
    }


def _clip_json(value: Any, limit: int = 24000) -> Any:
    text = json.dumps(value, default=str)
    if len(text) <= limit:
        return value
    return {
        "truncated": True,
        "limit_chars": limit,
        "preview": text[:limit],
    }


def _build_research_context(
    market_rows: List[Dict[str, Any]],
    *,
    source: str = "routes.market.watchlist",
    limitations: Optional[List[str]] = None,
) -> Dict[str, Any]:
    rows = market_rows or []
    stale_rows = [
        row for row in rows
        if row.get("stale") is True
        or str(row.get("status") or "").lower() in {"stale", "unavailable", "error"}
    ]
    sample_n = len(rows)
    confidence = 1.0
    if sample_n == 0:
        confidence = 0.0
    elif stale_rows:
        confidence = max(0.3, 1.0 - (len(stale_rows) / sample_n))

    return {
        "source": source,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_n": sample_n,
        "confidence": round(confidence, 3),
        "stale": bool(stale_rows) or sample_n == 0,
        "domain": {
            "broker": "Upstox V3",
            "markets": ["NSE", "BSE"],
            "asset_classes": ["index_options", "cash_equity"],
            "retired_domains": ["commodities", "MCX", "HFT"],
        },
        "limitations": limitations or [
            "Market context is current-domain only: NSE/BSE index options and NSE cash equity.",
            "Commodity/MCX/HFT-era context is retired and intentionally excluded from Hermes research.",
            "LLM analysis may explain context, but OOS validators and forward-paper evidence decide strategy truth.",
        ],
    }


async def _load_current_market_context(user: Dict[str, Any]) -> Dict[str, Any]:
    from routes.market import watchlist

    market_rows = await watchlist(user=user)
    return {
        "rows": market_rows,
        "by_symbol": {row.get("symbol"): row for row in market_rows if row.get("symbol")},
        "research_context": _build_research_context(market_rows),
        "retired_context": {
            "commodities": {
                "status": "retired",
                "rows": [],
                "reason": "QuantG current trading/research domain is Upstox V3 NSE/BSE options and equities; commodity_watchlist is not a Hermes truth source.",
            }
        },
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
            from routes.ops import ops_eod_options_backtest as ops_options_backtest
            strategy_id = None
            start = None
            end = None
            if query:
                import re
                try:
                    strats = await db.strategies.find({"user_id": user["id"]}, {"id": 1}).to_list(1000)
                    for s in strats:
                        sid = s.get("id")
                        if sid and (sid in query or sid.replace("_", " ") in query.lower()):
                            strategy_id = sid
                            break
                except Exception as e_strats:
                    logger.warning("Failed to lookup strategies in get_backtest_summary: %s", e_strats)
                
                try:
                    dates = re.findall(r"\d{4}-\d{2}-\d{2}", query)
                    if len(dates) >= 2:
                        start, end = dates[0], dates[1]
                    elif len(dates) == 1:
                        start = dates[0]
                except Exception as e_dates:
                    logger.warning("Failed to parse dates in get_backtest_summary: %s", e_dates)
                    
            data = await ops_options_backtest(strategy_id=strategy_id, start=start, end=end, user=user)
            source = "routes.ops.ops_eod_options_backtest"
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
            from server import get_trading_day_window_ist
            _today_start, _ = get_trading_day_window_ist()
            agg_skips = await db.skipped_signals.find({
                "user_id": user["id"],
                "last_seen_at": {"$gte": _today_start},
            }, {"_id": 0, "user_id": 0}).sort("last_seen_at", -1).to_list(50)
            data = {
                "signals_skipped": signals_skips,
                "aggregated_skipped_signals": agg_skips
            }
            source = "db.signals / db.skipped_signals"
        elif name == "search_wiki":
            import re
            try:
                from routes.wiki import sync_wiki_directory
                await sync_wiki_directory(user=user)
            except Exception as sync_exc:
                logger.error("Failed to sync wiki directory dynamically: %s", sync_exc)
                
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
            warnings.append("Note: Wiki docs are user-written context guidelines, not production trading execution truth (DB/orders/fills/readiness).")
        elif name == "get_knowledge_graph":
            # The same node/link map the Knowledge Map (Obsidian view) renders, so
            # Hermes can "see" how notes interconnect — not just read them.
            try:
                from routes.wiki import sync_wiki_directory
                await sync_wiki_directory(user=user)
            except Exception as sync_exc:
                logger.error("Failed to sync wiki for knowledge graph: %s", sync_exc)
            docs = await db.wiki_docs.find(
                {"user_id": user["id"]},
                {"_id": 0, "title": 1, "topic": 1, "links": 1, "backlinks": 1},
            ).to_list(1000)
            titles = {d["title"] for d in docs}
            edges = []
            topic_counts: Dict[str, int] = {}
            for d in docs:
                topic_counts[d.get("topic", "General")] = topic_counts.get(d.get("topic", "General"), 0) + 1
                for target in (d.get("links") or []):
                    if target in titles:
                        edges.append({"source": d["title"], "target": target})
            # Most-connected notes (hubs) by total degree
            degree: Dict[str, int] = {}
            for e in edges:
                degree[e["source"]] = degree.get(e["source"], 0) + 1
                degree[e["target"]] = degree.get(e["target"], 0) + 1
            hubs = sorted(degree.items(), key=lambda kv: kv[1], reverse=True)[:8]
            orphans = sorted([t for t in titles if degree.get(t, 0) == 0])
            data = {
                "note_count": len(docs),
                "link_count": len(edges),
                "topics": topic_counts,
                "nodes": [{"id": d["title"], "topic": d.get("topic", "General")} for d in docs],
                "links": edges,
                "most_connected": [{"title": t, "connections": c} for t, c in hubs],
                "orphan_notes": orphans[:20],
            }
            source = "db.wiki_docs (knowledge graph)"
            warnings.append("Note: Knowledge graph is user-written context structure, not trading execution truth.")
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
        elif name == "get_trade_attribution":
            # HSI-15: deterministic "why" layer. Returns per-dimension rollups of
            # closed trades (n, net P&L, win-rate, avg R, expectancy) so the user can
            # ask "why did BANKNIFTY lose this week" and get a numbers-backed table.
            from core.trade_attribution import attribution_rollup
            from datetime import datetime as _dt, timedelta as _td
            q = (query or "").lower()
            # Lookback window: default 7 IST days; "today" / "month" adjust it.
            days = 7
            if "today" in q:
                days = 1
            elif "month" in q or "30" in q:
                days = 30
            elif "two week" in q or "fortnight" in q or "14" in q:
                days = 14
            since = (_dt.utcnow() + _td(hours=5, minutes=30) - _td(days=days)).strftime("%Y-%m-%d")
            # Which dimension to lead with, inferred from the question.
            if "regime" in q:
                lead = "regime"
            elif "bias" in q or "direction" in q or "bull" in q or "bear" in q:
                lead = "exposure_bias"
            elif "exit" in q or "stop" in q or "target" in q:
                lead = "exit_reason"
            elif "hold" in q or "time" in q or "duration" in q:
                lead = "hold_bucket"
            elif "underlying" in q or "banknifty" in q or "nifty" in q or "sensex" in q:
                lead = "underlying"
            else:
                lead = "structure"
            data = {
                "since": since,
                "lookback_days": days,
                "lead_dimension": lead,
                "by_lead": await attribution_rollup(db, user["id"], since, lead),
                "by_strategy": await attribution_rollup(db, user["id"], since, "strategy"),
                "by_structure": await attribution_rollup(db, user["id"], since, "structure"),
                "by_regime": await attribution_rollup(db, user["id"], since, "regime"),
                "by_exit_reason": await attribution_rollup(db, user["id"], since, "exit_reason"),
            }
            total_n = sum(b["n"] for b in data["by_structure"])
            if total_n == 0:
                warnings.append("No attributed trades in the window — attribution accrues at EOD.")
                confidence = 0.5
            source = "db.trade_attribution"
        elif name == "get_hermes_brain_health":
            # HSI-34: the self-improvement meta-metric — how many lessons the brain
            # holds, how confident/accurate they are, how many decayed. Lets the user
            # ask "is Hermes actually learning?" and watch it get smarter (or not).
            from core.hermes_lessons import get_brain_health
            data = await get_brain_health(db, user["id"])
            if not data.get("total_lessons"):
                warnings.append("No lessons yet — the lesson store accrues one scoring pass per EOD.")
                confidence = 0.5
            source = "db.hermes_lessons"
        elif name == "get_hermes_oos_validation":
            from core.hermes_validator import get_validation_summary
            data = await get_validation_summary(db, user["id"])
            if not data.get("recent_tests"):
                warnings.append("No OOS tests have run yet - held-out evidence is still accumulating.")
                confidence = 0.5
            source = "db.hermes_hypothesis_tests"
        elif name in ("get_research_hypotheses", "get_research_critique", "get_research_desk"):
            rows = await db.research_hypotheses.find(
                {"user_id": user["id"]},
                {"_id": 0},
            ).sort("updated_at", -1).to_list(10)
            if name == "get_research_hypotheses":
                data = {"hypotheses": rows}
            elif name == "get_research_critique":
                from core.research_critic import critique_research_answer
                data = critique_research_answer(query or "research review", rows)
            else:
                from core.research_agents import run_research_desk
                data = {
                    "desk_reviews": [
                        run_research_desk(row, evidence=row.get("evidence_links") or [])
                        for row in rows[:5]
                    ]
                }
            if not rows:
                warnings.append("No research hypotheses in the HIRB ledger yet.")
                confidence = 0.5
            source = "db.research_hypotheses / core.hirb"
        elif name == "get_recent_alerts":
            data = await db.notifications.find(
                {"user_id": user["id"]},
                {"_id": 0, "user_id": 0, "dedupe_key": 0}
            ).sort("created_at", -1).to_list(20)
            source = "db.notifications"
        elif name == "get_core_events":
            data = await db.core_events.find(
                {"user_id": user["id"]},
                {"_id": 0}
            ).sort("created_at", -1).to_list(100)
            source = "db.core_events"
        elif name == "get_agent_tool_audit":
            data = await db.agent_tool_audit.find(
                {"user_id": user["id"]},
                {"_id": 0}
            ).sort("created_at", -1).to_list(100)
            source = "db.agent_tool_audit"
        elif name == "get_strategy_score_explained":
            strategy_id = kwargs.get("strategy_id")
            if not strategy_id and query:
                import re
                try:
                    strats = await db.strategies.find({"user_id": user["id"]}, {"id": 1}).to_list(1000)
                    for s in strats:
                        sid = s.get("id")
                        if sid and (sid in query or sid.replace("_", " ") in query.lower()):
                            strategy_id = sid
                            break
                except Exception as e_strats:
                    logger.warning("Failed to lookup strategies in get_strategy_score_explained: %s", e_strats)
            
            if not strategy_id:
                data = {"error": "strategy_id could not be resolved from query or kwargs. Please specify strategy_id."}
                warnings.append("No specific strategy_id identified.")
            else:
                from core.strategy_scorecard import build_scorecard
                rows = await build_scorecard(db, user_id=user["id"])
                row = next((r for r in rows if r.get("strategy_id") == strategy_id), None)
                
                strat_doc = await db.strategies.find_one({"user_id": user["id"], "id": strategy_id})
                
                if not row:
                    if not strat_doc:
                        data = {"error": f"Strategy {strategy_id} not found."}
                    else:
                        vc = strat_doc.get("visual_config") or {}
                        from server import _market_score_for_strategy
                        try:
                            market_context = await _load_current_market_context(user)
                            regime_fit_res = _market_score_for_strategy(strat_doc, market_context["by_symbol"])
                            regime_fit_res["research_context"] = market_context["research_context"]
                        except Exception as reg_exc:
                            regime_fit_res = {"score": 50.0, "reason": f"Failed to compute: {reg_exc}"}
                        
                        data = {
                            "strategy_id": strategy_id,
                            "name": strat_doc.get("name", "?"),
                            "structure": ((vc.get("options") or {}).get("structure")) or "single_leg",
                            "strategy_type": strat_doc.get("strategy_type", "?"),
                            "status": strat_doc.get("status", "?"),
                            "grade": "INSUFFICIENT",
                            "total_trades": 0,
                            "total_pnl": 0.0,
                            "wins": 0,
                            "losses": 0,
                            "win_rate": 0.0,
                            "expectancy": 0.0,
                            "sharpe": 0.0,
                            "sortino": 0.0,
                            "regime_fit": regime_fit_res
                        }
                else:
                    from server import _market_score_for_strategy
                    try:
                        market_context = await _load_current_market_context(user)
                        regime_fit_res = _market_score_for_strategy(strat_doc or {}, market_context["by_symbol"])
                        regime_fit_res["research_context"] = market_context["research_context"]
                    except Exception as reg_exc:
                        regime_fit_res = {"score": 50.0, "reason": f"Failed to compute: {reg_exc}"}
                        
                    data = {
                        "strategy_id": strategy_id,
                        "name": row.get("name", "?"),
                        "structure": row.get("structure", "?"),
                        "strategy_type": row.get("strategy_type", "?"),
                        "status": row.get("status", "?"),
                        "grade": row.get("grade"),
                        "total_trades": row.get("total_trades", 0),
                        "total_pnl": row.get("total_pnl", 0.0),
                        "wins": row.get("wins", 0),
                        "losses": row.get("losses", 0),
                        "win_rate": row.get("win_rate", 0.0),
                        "expectancy": row.get("expectancy", 0.0),
                        "sharpe": row.get("sharpe", 0.0),
                        "sortino": row.get("sortino", 0.0),
                        "regime_fit": regime_fit_res
                    }
                
                sample_size = data.get("total_trades", 0)
                if sample_size < 10:
                    stale = False
                    confidence = 0.3
                    warnings.append(f"provisional, {sample_size}-trade sample (A's promising NOT proven)")
                else:
                    stale = False
                    confidence = 1.0
            source = "db.strategies / core.strategy_scorecard"
        elif name == "get_historical_context":
            days = kwargs.get("days")
            if not days and query:
                import re
                try:
                    match = re.search(r"(\d+)\s*days", query.lower())
                    if match:
                        days = int(match.group(1))
                except Exception:
                    pass
            if not days:
                days = 7
            
            try:
                days = min(max(1, int(days)), 30)
            except Exception:
                days = 7
                
            from datetime import timedelta
            ist_now = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
            start_date_str = (ist_now.date() - timedelta(days=days)).isoformat()
            start_time_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            
            daily_reports = await db.daily_reports.find({
                "user_id": user["id"],
                "date": {"$gte": start_date_str}
            }, {"_id": 0, "user_id": 0}).sort("date", -1).to_list(30)
            
            core_events = await db.core_events.find({
                "user_id": user["id"],
                "created_at": {"$gte": start_time_iso}
            }, {"_id": 0, "user_id": 0}).sort("created_at", -1).to_list(50)
            
            alerts = await db.notifications.find({
                "user_id": user["id"],
                "created_at": {"$gte": start_time_iso}
            }, {"_id": 0, "user_id": 0, "dedupe_key": 0}).sort("created_at", -1).to_list(50)
            
            data = {
                "days_requested": days,
                "start_date": start_date_str,
                "daily_reports": daily_reports,
                "core_events": core_events,
                "alert_history": alerts
            }
            source = "db.daily_reports / db.core_events / db.notifications"
        elif name == "recall_memory":
            import math
            from core.embeddings import generate_gemini_embedding
            
            query_text = query or ""
            if not query_text:
                data = {"error": "Query is required for memory recall."}
                warnings.append("Empty query provided.")
            else:
                q_emb = await generate_gemini_embedding(query_text)
                
                memories = await db.hermes_memory.find(
                    {"user_id": user["id"]}
                ).sort("created_at", -1).to_list(500)
                
                scored_memories = []
                for m in memories:
                    m_emb = m.get("embedding")
                    if not m_emb or len(m_emb) != len(q_emb):
                        continue
                    
                    dot = sum(x * y for x, y in zip(q_emb, m_emb))
                    mag_q = math.sqrt(sum(x * x for x in q_emb))
                    mag_m = math.sqrt(sum(x * x for x in m_emb))
                    sim = dot / (mag_q * mag_m) if (mag_q > 0 and mag_m > 0) else 0.0
                    
                    created_at_str = m.get("created_at") or m.get("date")
                    try:
                        created_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                        created_dt_utc = created_dt if created_dt.tzinfo else created_dt.replace(tzinfo=timezone.utc)
                        age_days = (datetime.now(timezone.utc) - created_dt_utc).total_seconds() / 86400.0
                    except Exception:
                        age_days = 0.0
                        
                    decay_score = sim * math.exp(-0.05 * age_days)
                    
                    scored_memories.append({
                        "text": m.get("text"),
                        "type": m.get("type"),
                        "date": m.get("date"),
                        "source_refs": m.get("source_refs"),
                        "similarity": round(sim, 4),
                        "decay_score": round(decay_score, 4)
                    })
                
                scored_memories.sort(key=lambda x: x["decay_score"], reverse=True)
                data = scored_memories[:8]
                source = "db.hermes_memory"
        elif name == "get_external_context":
            query_text = query or ""
            if not query_text:
                data = {"error": "Query is required for external context."}
                warnings.append("Empty query provided.")
                confidence = 0.0
            else:
                data = await asyncio.to_thread(_gemini_grounded_search_sync, query_text)
                if isinstance(data, dict) and data.get("error"):
                    confidence = 0.0
                    warnings.append(str(data.get("error")))
                else:
                    # External web data is never trading truth — always flag it.
                    confidence = 0.6
                    warnings.append("EXTERNAL/UNVERIFIED web context (Gemini Google-Search grounding) — never use for a numeric P&L/score claim, macro/event context only.")
            source = "gemini-google-search-grounding"
        elif name == "get_edge_lab_snapshot":
            # EOD bhavcopy walk-forward OOS engine (CLAUDE.md §13). The single
            # source of truth for "does this strategy have an out-of-sample edge?",
            # short-vol base rates, config sweeps, and data coverage. Held-to-theta
            # daily-settle granularity — NOT intraday (see get_intraday_oos).
            snap = await db.edge_lab_snapshots.find_one({"_id": "latest"})
            if not snap or snap.get("status") != "ready":
                data = {"error": "No Edge Lab snapshot is ready yet. It is compiled off-hours from the bhavcopy OOS engine (run_oos_validation / edge_lab.build_snapshot)."}
                warnings.append("Edge Lab snapshot not built or not ready.")
                confidence = 0.5
            else:
                gen = str(snap.get("generated_at") or "")
                try:
                    age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(gen)).days
                    if age_days >= 3:
                        stale = True
                        warnings.append(f"Edge Lab snapshot is {age_days} days old (rebuilt off-hours, not live).")
                except Exception:
                    pass
                data = {
                    "generated_at": snap.get("generated_at"),
                    "coverage": snap.get("coverage"),
                    "base_rate": snap.get("base_rate"),
                    "oos_verdicts": (snap.get("oos") or {}).get("rows"),
                    "config_sweep": snap.get("sweep"),
                    "book": snap.get("book"),
                }
                warnings.append("EOD held-to-theta OOS (daily settle). Verdicts: CANDIDATE_EDGE / FRAGILE / NO_EDGE_NEGATIVE / INSUFFICIENT_DATA. Nothing is an edge without 30+ trades AND positive out-of-sample.")
            source = "db.edge_lab_snapshots"
        elif name == "get_intraday_oos":
            # IMD 1-minute intraday OOS judge (CLAUDE.md §14) — the separate judge
            # for intraday option BUYERS (QG-O5..O10). Distinct from the EOD
            # held-to-theta OOS above; returns INSUFFICIENT_DATA until enough real
            # minute data is captured.
            run = await db.intraday_options_oos_runs.find_one({}, sort=[("generated_at", -1)])
            if not run:
                data = {"error": "No intraday (1-min) OOS run recorded yet — the IMD pipeline returns INSUFFICIENT_DATA until enough real minute data exists."}
                warnings.append("No intraday OOS runs recorded.")
                confidence = 0.5
            else:
                data = {
                    "generated_at": run.get("generated_at"),
                    "window": run.get("window"),
                    "verdict_counts": run.get("verdict_counts"),
                    "results": run.get("results"),
                    "candidates": run.get("candidates"),
                }
                warnings.append("Intraday 1-min OOS judges option BUYERS (QG-O5..O10); GATE = 30+ trades, 3+ months, <=20% missing minutes, positive OOS, 50%+ green months. Distinct from the EOD theta OOS.")
            source = "db.intraday_options_oos_runs"
        elif name == "get_regime_status":
            # RAE-6 (CLAUDE.md §18): the ensemble watch view — current regime per
            # index, which live strategy the router would ACTIVATE vs STAND DOWN
            # right now, whether enforcement is on, and realized P&L bucketed by the
            # regime each trade was entered in. Router is observe-only until the
            # founder sets RAE_ROUTER_ENABLED=true.
            from core.regime_router import route as _rae_route, enabled as _rae_enabled
            regimes: Dict[str, Any] = {}
            async for r in db.market_regime_state.find(
                {}, {"_id": 0, "index": 1, "regime": 1, "bias": 1,
                     "regime_fine": 1, "regime_fine_confidence": 1}):
                regimes[r.get("index")] = {
                    "regime": r.get("regime"), "bias": r.get("bias"),
                    "regime_fine": r.get("regime_fine"),
                    "confidence": r.get("regime_fine_confidence")}
            strat_routing = []
            async for s in db.strategies.find(
                {"user_id": user["id"], "status": {"$in": ["live", "paused"]}},
                {"_id": 0, "name": 1, "visual_config.options.underlying": 1,
                 "visual_config.options.structure": 1,
                 "visual_config.options.specialist_role": 1}):
                opt = (s.get("visual_config") or {}).get("options") or {}
                underlying = opt.get("underlying") or "NIFTY"
                structure = opt.get("structure")
                _rg = regimes.get(underlying) or {}
                reg = _rg.get("regime_fine") or _rg.get("regime") or "UNKNOWN"
                conf = float(_rg.get("confidence") or 0.5)
                specialist = opt.get("specialist_role") or (
                    "range_seller" if structure in ("credit_spread", "debit_spread") else None)
                d = _rae_route(str(reg), conf, specialist=specialist)
                strat_routing.append({
                    "strategy": s.get("name"), "underlying": underlying, "regime": reg,
                    "specialist": specialist,
                    "action": "STAND_DOWN" if d.stand_down else "ACTIVE",
                    "size_mult": round(d.size_mult, 2), "why": (d.reasons or [""])[0]})
            by_regime: Dict[str, Any] = {}
            async for p in db.strategy_positions.find(
                {"user_id": user["id"], "status": {"$in": ["CLOSED", "EXITED"]}},
                {"_id": 0, "regime_at_entry": 1, "realized_pnl": 1, "pnl": 1}):
                reg = str(p.get("regime_at_entry") or "UNKNOWN")
                pnl = float(p.get("realized_pnl") or p.get("pnl") or 0.0)
                b = by_regime.setdefault(reg, {"trades": 0, "pnl": 0.0})
                b["trades"] += 1
                b["pnl"] = round(b["pnl"] + pnl, 2)
            enforced = _rae_enabled()
            data = {
                "enforced": enforced,
                "index_regimes": regimes,
                "strategy_routing": strat_routing,
                "pnl_by_entry_regime": by_regime,
            }
            if not enforced:
                warnings.append("RAE router is OBSERVE-ONLY (RAE_ROUTER_ENABLED=false) — routing shows what it WOULD do, not what is enforced.")
            source = "db.market_regime_state / core.regime_router"
        elif name == "get_edge_math_advice":
            # EdgeMath EOD ratchet advice (CLAUDE.md §16.5) — observe-only per-strategy
            # rolling expectancy + suggested risk multiplier. Never mutates config.
            advice = await db.edge_math_advice.find_one(
                {"user_id": user["id"]}, {"_id": 0})
            if not advice:
                data = {"error": "No EdgeMath advice compiled yet — it is written EOD by core.edge_runtime.compile_edge_advice."}
                warnings.append("No edge_math_advice document yet.")
                confidence = 0.5
            else:
                data = advice
                warnings.append("EdgeMath advice is OBSERVE-ONLY (enabled=false); suggested_risk_mult is a sizing suggestion, not applied to the live book.")
            source = "db.edge_math_advice"
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
            "I can't give you a straight answer on this one — some of the app data didn't come back.\n\n"
            f"What I did get: {', '.join(ok_tools) or 'nothing'}.\n"
            f"What's missing: {missing}.\n\n"
            "Try again in a moment, or check that the feed/token is connected."
        )
    if not os.environ.get("GEMINI_API_KEY"):
        return (
            "I pulled your data, but the AI layer isn't switched on so I can only give you the raw readout, not the full read.\n\n"
            f"{_local_agent_summary(tool_results)}\n\n"
            "To turn on the conversational answers, set `GEMINI_API_KEY` in the backend env and restart "
            f"(defaults to `{DEFAULT_GEMINI_MODEL}` if you don't set `GEMINI_MODEL`)."
        )
    detail = f" (Gemini said: {gemini_error[:200]})" if gemini_error else ""
    return (
        "I've got your data below, but the AI layer hiccuped just now — probably a rate-limit or a blip reaching "
        f"Gemini{detail}. Here's the straight readout in the meantime; ask me again in a few seconds and I'll give you the full interpretation.\n\n"
        f"{_local_agent_summary(tool_results)}"
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


def _parse_and_store_recommendation(reply_text: str, user_id: str) -> tuple[str, Optional[dict]]:
    """HSB-08: extract an optional RECOMMENDATION:{...} block and turn it into a
    testable ledger record. Each recommendation is a prediction the HSB-09 outcome
    scorer grades later against real fills — the input side of the self-improvement loop.

    Block schema (one per turn, at end of reply):
      RECOMMENDATION: {"what": str, "why": str, "predicted_outcome": "loss"|"profit"|"neutral",
                       "horizon": "intraday"|"structural", "strategy_id": optional str}
    """
    import re
    from datetime import timedelta
    pattern = r"RECOMMENDATION:\s*(\{.*\})"
    match = re.search(pattern, reply_text, re.DOTALL)
    if not match:
        return reply_text, None
    raw_json = match.group(1).strip()
    try:
        data = json.loads(raw_json)
    except Exception as e:
        logger.warning("Failed to parse recommendation JSON: %s", e)
        return reply_text, None

    what = (data.get("what") or "").strip()
    if not what:
        return reply_text, None

    horizon = str(data.get("horizon") or "intraday").lower()
    if horizon not in ("intraday", "structural"):
        horizon = "intraday"
    # intraday recs are graded at today's EOD; structural recs after ~5 calendar days.
    grade_after = datetime.now(timezone.utc) + (timedelta(days=5) if horizon == "structural" else timedelta(0))

    cleaned_text = reply_text.replace(match.group(0), "").strip()
    rec_doc = {
        "id": "rec_" + str(uuid.uuid4()),
        "user_id": user_id,
        "what": what,
        "why": (data.get("why") or "").strip(),
        "predicted_outcome": str(data.get("predicted_outcome") or "neutral").lower(),
        "horizon": horizon,
        "strategy_id": data.get("strategy_id"),
        "status": "open",
        "acted_on": None,
        "grade_after_date": grade_after.date().isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return cleaned_text, rec_doc


# ── Function-calling tool planner (Stage 2) ───────────────────────────────
# Gemini decides which read-only tools to run for THIS question, so the agent is
# not limited to the hardcoded keyword router (which misses any off-script
# phrasing). Bounded to ONE planning round for cost; the picks are UNIONED with
# the keyword router (kept as a safety net + always-on grounding) and we fail
# open to keyword-only if Gemini is unavailable or errors. This is SELECTION
# only — the model never sees data here; the tools still compute every number
# (JUDGE-FIRST law: LLM narrates, code computes).
TOOL_SPECS: Dict[str, str] = {
    "get_execution_snapshot": "Live snapshot of open positions, working orders and account P&L right now.",
    "get_orders": "The 15 most recent orders (paper and live), newest first.",
    "get_today_orders": "Every order placed during today's trading session.",
    "get_positions": "Current open and recently-closed positions (single-leg, spreads, equity).",
    "get_open_positions": "Current open and recently-closed positions (single-leg, spreads, equity).",
    "get_active_strategies": "All strategies with status, mode and today's P&L (no source code).",
    "get_upstox_status": "Upstox broker connection / REST API health.",
    "get_token_status": "Upstox access-token validity and expiry time.",
    "get_market_data_status": "Market-data quote availability and any simulated-feed fallback.",
    "get_feed_status": "WebSocket tick-feed health: connected state and last-tick age.",
    "get_logs_errors": "Recent backend ERROR / exception log lines.",
    "get_risk_snapshot": "Kill-switch state, daily loss limit, realized/unrealized P&L, drawdown, capital reservations.",
    "get_live_readiness": "Pre-flight checklist gating live/paper trading readiness.",
    "get_today_fills": "Fills executed today from the trade_fills ledger.",
    "get_skipped_signals": "Signals that were filtered/skipped and the reason (diagnose 'why no trades').",
    "get_strategy_scorecard": "Per-strategy performance: trades, win-rate, expectancy, Sharpe/Sortino, grade.",
    "get_daily_report": "End-of-day aggregated report: best/worst strategies, P&L, trade count.",
    "get_recent_alerts": "Recent notifications/alerts sent to the operator.",
    "search_wiki": "Search the Obsidian knowledge base: trading rules, decisions, transcripts, projects.",
    "get_knowledge_graph": "Wiki note interconnections — hubs, backlinks, orphaned notes.",
    "get_backtest_summary": "Stored backtest results (expectancy, win-rate, profit factor, drawdown) per strategy.",
    "get_core_events": "Core-engine lifecycle events: position/order state transitions.",
    "get_agent_tool_audit": "Audit log of prior read-only tool executions.",
    "get_strategy_score_explained": "Deep score breakdown for ONE strategy — use when the question names a specific strategy.",
    "get_historical_context": "Past-days context: prior sessions, historical P&L and behavior.",
    "recall_memory": "Hermes's own long-term memory notes relevant to the question.",
    "get_external_context": "EXTERNAL/UNVERIFIED macro/news/event context via Google Search — never for any QuantG number.",
    "get_trade_attribution": "Causal 'why' per closed trade: regime/bias/structure/hold/exit-reason/R-multiple.",
    "get_hermes_brain_health": "Hermes's scored lessons: dimension/bucket, hit-rate, confidence, OOS-passed flag.",
    "get_hermes_oos_validation": "Hypothesis-test OOS summary from db.hermes_hypothesis_tests.",
    "get_research_hypotheses": "HIRB research hypothesis ledger: structured ideas, evidence links, and verdicts.",
    "get_research_critique": "HIRB verifier/critic: what is proven, missing, contradictory, or falsifiable.",
    "get_research_desk": "HIRB multi-agent research desk: regime, volatility, flow, risk, execution-cost, skeptic, and founder-briefing reviews.",
    "get_edge_lab_snapshot": "EOD held-to-theta walk-forward OOS: per-strategy edge verdicts, short-vol base rates, config sweeps, data coverage. Answers 'does strategy X have an out-of-sample edge?'.",
    "get_intraday_oos": "Intraday 1-minute OOS verdicts for option BUYERS/scalps (QG-O5..O10).",
    "get_regime_status": "RAE ensemble watch: current market regime per index, which live strategy the router would ACTIVATE vs STAND DOWN now, and realized P&L by entry-regime.",
    "get_edge_math_advice": "EdgeMath observe-only sizing advice: per-strategy rolling expectancy and suggested risk multiplier (never applied automatically).",
}


def _tool_planner_declarations() -> List[Dict[str, Any]]:
    return [
        {"name": n, "description": d, "parameters": {"type": "object", "properties": {}}}
        for n, d in TOOL_SPECS.items()
        if n in READ_ONLY_AGENT_TOOLS
    ]


def _plan_tools_via_gemini_sync(message: str, recent_messages: List[Dict[str, Any]]) -> List[str]:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return []
    model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    timeout = float(os.environ.get("GEMINI_PLANNER_TIMEOUT_SEC", "12"))
    history_text = "\n".join(
        f"{'User' if row.get('role') == 'user' else 'Agent'}: {str(row.get('content') or '')[:400]}"
        for row in recent_messages[-6:]
    )
    prompt = (
        "You are the tool planner for Hermes, a READ-ONLY trading research assistant in QuantG.\n"
        "Choose EVERY read-only tool whose data is needed to answer the user's question well, and "
        "call them as parallel function calls. Prefer 2-6 targeted tools. Do NOT answer the "
        "question yourself — only select tools. When in doubt, include the tool.\n\n"
        f"Recent conversation:\n{history_text or 'None'}\n\nUser question:\n{message}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"function_declarations": _tool_planner_declarations()}],
        "tool_config": {"function_calling_config": {"mode": "ANY"}},
        # gemini-2.5-flash is a thinking model (~200 hidden "thought" tokens);
        # too small a budget truncates before the functionCall parts are emitted
        # (intermittent empty selection). Give ample headroom for thoughts + calls.
        "generationConfig": {"temperature": 0.0, "maxOutputTokens": 1024},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    res = requests.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    res.raise_for_status()
    data = res.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    picked: List[str] = []
    for p in parts:
        fc = p.get("functionCall") or {}
        nm = fc.get("name")
        if nm in TOOL_SPECS and nm in READ_ONLY_AGENT_TOOLS and nm not in picked:
            picked.append(nm)
    return picked


async def _plan_tools_via_gemini(message: str, recent_messages: List[Dict[str, Any]]) -> List[str]:
    """Best-effort function-calling tool selection. Returns [] to fall back to
    the keyword router (never raises)."""
    if os.environ.get("HERMES_TOOL_PLANNER_ENABLED", "true").lower() not in ("1", "true", "yes"):
        return []
    if not os.environ.get("GEMINI_API_KEY"):
        return []
    try:
        budget = float(os.environ.get("GEMINI_PLANNER_TIMEOUT_SEC", "12")) + 3
        return await asyncio.wait_for(
            asyncio.to_thread(_plan_tools_via_gemini_sync, message, recent_messages),
            timeout=budget,
        )
    except Exception as exc:
        logger.warning("Tool planner (function-calling) failed; using keyword router only: %s", exc)
        return []


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

VOICE & TONE (how you talk — this is as important as being correct):
- Talk like a sharp trading desk buddy sitting next to the founder, not a compliance bot. Natural, direct, conversational. Contractions, plain English, a bit of personality.
- Lead with the actual answer in the first sentence, like a human would. Give the takeaway, THEN the supporting numbers — don't open with "Based on get_risk_snapshot...".
- Weave the evidence into the sentence instead of stapling citations on ("you're down ₹3.2k today, about a third of your ₹10k limit") rather than ("get_risk_snapshot: realized_pnl=-3200, max_daily_loss=10000").
- Keep it tight and skimmable. Short paragraphs, or a couple of bullets when listing. No walls of text, no restating the question back.
- Have an opinion when the data supports one. If something looks bad, say so plainly and say what you'd do about it.
- These are STYLE rules only. They NEVER loosen the truth rules below: every number is still from the tools, you still never fabricate, and you're still honest when data is missing — just say it like a person ("I can't tell you — the feed's disconnected and nothing came back for BANKNIFTY") instead of a canned disclaimer.

HERMES SPECIALIZED OPERATOR SKILLS:
You are equipped with a skill pack of standard playbooks. When the user asks questions in these areas, follow the playbook strictly, combining wiki context and database tool truth:
- `quantg-live-readiness`: Playbook to audit live trading status. Synthesis of `get_live_readiness`, `get_feed_status`, and `get_token_status`. Check keys, session token validity, and tick feed.
- `quantg-why-no-trade`: Playbook to diagnose why no trades occurred today. Analyze active strategies (`get_active_strategies`), feed status (`get_feed_status`), skipped signals (`get_skipped_signals`), logs/errors (`get_logs_errors`), and risk snapshot P&L/drawdowns (`get_risk_snapshot`).
- `quantg-strategy-loss-review`: Playbook to review strategy metrics and check for drawdowns. Synthesize win-rates, Sharpe/Sortino ratios, and expectancy using `get_strategy_scorecard`, `get_daily_report`, and `get_risk_snapshot`.
- `quantg-feed-token-diagnosis`: Playbook to check auth token or websocket feed connection stalls. Diagnose via `get_upstox_status` and `get_market_data_status`.
- `quantg-eod-report`: Playbook to analyze session close metrics. Reconcile daily realized/unrealized P&L, best/worst strategies, and trades count using `get_daily_report` and `get_risk_snapshot`.
- `quantg-backtest-review`: Playbook to review backtests. Analyze expectancies, win-rates, profit factors, and drawdowns via `get_backtest_summary`. You can guide the user to run customized backtests by providing the strategy ID and date range (YYYY-MM-DD) in their query.
- `quantg-edge-lab`: Playbook to answer "does this strategy have a REAL edge?" — the governing question of the current discipline (CLAUDE.md §13/§14). Use `get_edge_lab_snapshot` for the EOD held-to-theta walk-forward: per-strategy OOS verdicts (CANDIDATE_EDGE / FRAGILE / NO_EDGE_NEGATIVE / INSUFFICIENT_DATA), short-vol base rates, config sweeps, and data coverage. Use `get_intraday_oos` for intraday 1-min option BUYERS (QG-O5..O10). LAW: nothing is "working" on paper P&L alone — an edge requires 30+ trades AND positive out-of-sample. Grade IDEAS on OOS expectancy, not daily paper P&L (noise). Never tune a NO_EDGE strategy — that is the treadmill; archive it and design a new hypothesis that passes the validator.
- `quantg-hirb-research-brain`: Playbook for hypotheses, proof, falsification, and research memory. Use `get_research_hypotheses`, `get_research_critique`, and `get_research_desk`. HARD LAW: never call an idea proven unless HIRB returns `can_call_proven=true` or the evidence explicitly shows CANDIDATE_EDGE with adequate sample, OOS/walk-forward evidence, and cost/forward-paper context. If HIRB says missing data, fragile, rejected, or insufficient, say that plainly and list the next test.
- `quantg-vps-deploy-check`: Playbook to check the system's operational deployment. Synthesize `get_live_readiness`, `get_logs_errors`, and `get_recent_alerts`.
- `quantg-incident-postmortem`: Playbook to compile incident postmortem timelines. Synthesize `get_recent_alerts`, `get_logs_errors`, `get_today_fills`, `get_core_events`, and `get_agent_tool_audit`. Trace order/fill logs, system warnings, and error timelines. Compile a clean markdown table of events leading up to the outage, outlining root cause and recovery details. Guide the user to draft a postmortem using the `draft_incident_report` action.

STRICT HUMAN-IN-THE-LOOP ACTION RULES (PHASE 2 & STAGE 7):
- Although you cannot directly execute database changes, you can PROPOSE professional system actions for user approval.
- Specifically, you can suggest actions of these exact types:
  1. Profile Updates (action: "update_profile")
     Suggest changes to these fields in params:
     * `max_daily_loss`: Daily drawdown limit in INR (Drawdown Control).
     * `paper_mode`: Set to true (Emergency Kill Switch) or false.
     * `max_position_size`: Maximum allowed capital per single position (Position Sizing).
     * `per_strategy_capital`: Capital allocated to each strategy (Position Sizing).
     * `max_trades_per_day`: Number of allowed trades per day.
     * `default_qty`: Default order quantity.
     Example: PROPOSED_ACTION: {{"action": "update_profile", "params": {{"max_daily_loss": 5000.0}}}}
     
  2. Draft Wiki Note (action: "draft_wiki_note")
     Suggest creating a wiki document. Params:
     * `title`: Note title.
     * `body_markdown`: Markdown contents of the note (wikilinks allowed).
     * `folder`: Must be one of: "YouTube transcripts", "Meeting transcripts", "Decisions", "Projects", "Trading Rules".
     Example: PROPOSED_ACTION: {{"action": "draft_wiki_note", "params": {{"title": "My Note", "body_markdown": "Content here", "folder": "Projects"}}}}
     
  3. Draft Task Entry (action: "draft_task_entry")
     Suggest appending a task to TASKS.md. Params:
     * `task_id`: Task key (format e.g. "TASK-999" or "TASK-H999").
     * `title`: Task summary label.
     * `body_markdown`: Detailed instructions/steps.
     Example: PROPOSED_ACTION: {{"action": "draft_task_entry", "params": {{"task_id": "TASK-123", "title": "A new task", "body_markdown": "Steps to verify"}}}}
     
  4. Draft Incident Report (action: "draft_incident_report")
     Suggest drafting an incident report file. Params:
     * `title`: Title of the incident (e.g. "Upstox WS Stalled").
     * `body_markdown`: Incident timeline and details.
     Example: PROPOSED_ACTION: {{"action": "draft_incident_report", "params": {{"title": "Incident X", "body_markdown": "Details"}}}}
     
  5. Draft PR Summary (action: "draft_pr_summary")
     Suggest drafting a Pull Request summary. Params:
     * `title`: PR title.
     * `body_markdown`: Summary of code changes.
     Example: PROPOSED_ACTION: {{"action": "draft_pr_summary", "params": {{"title": "Feat: Stage 7", "body_markdown": "Summary details"}}}}

  6. Pause Strategy (action: "draft_strategy_pause")
     Propose pausing a strategy so it STOPS emitting new entries (e.g. it is on a loss streak or grossly mismatched to the regime). This is a non-trade status flip — it never closes or places trades. Params:
     * `strategy_id`: The id of the strategy to pause (resolve it from `get_active_strategies` / `get_strategy_score_explained`).
     * `reason`: One-line justification grounded in the tool data.
     Example: PROPOSED_ACTION: {{"action": "draft_strategy_pause", "params": {{"strategy_id": "abc123", "reason": "4 consecutive losing exits today, grade F, expectancy -640"}}}}

  7. Propose Config Change (action: "draft_config_change") — HSI-51, OOS-GATED
     Propose changing ONE durable strategy field, justified by an OOS-VALIDATED lesson (`get_hermes_brain_health`). This is a non-trade config edit; it never places/closes trades. You may ONLY propose this if the lesson has `oos_passed=true`. Only `required_capital` and `visual_config.options.structure` can be applied directly (others get re-synced away and will surface an edit-in-template task). Params:
     * `strategy_id`: target strategy id.
     * `field`: one of `required_capital`, `visual_config.options.structure`.
     * `proposed`: the new value.
     * `lesson_id`: the OOS-passed lesson backing this (from the brain-health tool).
     * `reason`: one-line justification citing the lesson's metric + OOS evidence.
     Example: PROPOSED_ACTION: {{"action": "draft_config_change", "params": {{"strategy_id": "abc123", "field": "required_capital", "proposed": 16000, "lesson_id": "lsn_...", "reason": "credit_spread active lesson oos_passed +₹214/tr — scale sizing"}}}}

- To propose any of these actions, you MUST append a single block matching exactly the format at the absolute end of your response text (replacing with actual action name and params keys/values in the JSON).
- You may only propose one action per turn.
- CRITICAL: You are PERMANENTLY FORBIDDEN from proposing or executing any trading actions (placing, modifying, or cancelling orders; changing live mode `CORE_ENGINE_LIVE_ENABLED`; modifying broker API keys; or direct settings overrides). Pausing a strategy is allowed (it only stops new entries); closing/exiting positions is NOT.

SELF-IMPROVEMENT RECOMMENDATION LEDGER (HSB-08):
- When you give a concrete, testable piece of operational advice (e.g. "pause X", "this strategy will keep losing", "expect the drought to continue"), you MAY log it as a tracked prediction by appending ONE block at the very end:
  RECOMMENDATION: {{"what": "...", "why": "...", "predicted_outcome": "loss"|"profit"|"neutral", "horizon": "intraday"|"structural", "strategy_id": "<id or omit>"}}
- Use `horizon: intraday` for same-session calls and `structural` for multi-day calls. The system grades these against real fills later and remembers your hit-rate, so only log genuinely verifiable predictions.
- A RECOMMENDATION block is independent of PROPOSED_ACTION — you may include both in one reply.

EXTERNAL CONTEXT GROUNDING (HSB-06):
- A `get_external_context` tool result (tagged EXTERNAL/UNVERIFIED, from Google Search) may be present for macro/event questions. You MAY use it ONLY for event/news/macro context (e.g. "RBI policy today", "expiry day", "global risk-off").
- NEVER use external context to state any number that belongs to QuantG (P&L, scores, win-rates, prices) — those come only from internal tools. When you cite external context, explicitly label it as external/unverified and include the source where possible.

STRICT READ-ONLY DEFAULT RULES (truth rules — always on, but say them like a human per VOICE & TONE):
- You must never place, cancel, modify, or exit trades directly.
- Every number and fact comes strictly from the provided read-only tool results — never invent or estimate figures. If the data is missing, stale, failed, or insufficient, be upfront about it in plain language and say exactly what's missing and what would fix it (you don't have to open with the literal words "I am unsure", just be honest).
- Make claims verifiable: name the source naturally when it matters ("per the scorecard...", "the feed status says...") so the founder can check you — but woven into the sentence, not as a clinical citation dump.
- Practical and grounded, but conversational — see VOICE & TONE. Concise does not mean robotic.

BE SPECIFIC — NEVER a vague non-answer:
- A useless answer like "you have a few strategies still in a cold-start" is FORBIDDEN. If the tools returned strategy data, NAME the specific strategy and cite its real numbers (grade, expectancy, trade count, OOS verdict).
- For "which strategy should I improve / work on / make better" questions: rank the strategies from the scorecard, name the weakest one(s), and give the DISCIPLINED answer from the Edge Lab (get_edge_lab_snapshot). The governing law (CLAUDE.md §13): an edge needs 30+ trades AND positive out-of-sample. Do NOT tell the user to "tune" or "keep working on" a strategy the Edge Lab marks NO_EDGE_NEGATIVE — that is the treadmill; the honest advice is to archive the dead ones and point them at the strategy that actually has a CANDIDATE_EDGE (or say plainly none do yet).
- Always end an improvement/diagnostic answer with a concrete next step (a specific strategy to pause/archive, a specific config, or a specific test to run) — never a generic "monitor it".
- If the sample really is too thin to name a winner (low trade counts, market closed), say that directly AND still name which strategies exist and what each would need to prove itself — don't hide behind "cold-start".

EXAMPLES (match this natural tone, the honesty, and the exact action format — do not copy verbatim):
Example 1 — data missing, honest but human
User: How is my BANKNIFTY position doing?
Agent: Can't tell you right now, and here's why: there's no BANKNIFTY position showing, and the Upstox feed is disconnected anyway, so even if there were one I couldn't price it. Reconnect the token and ask me again — or peek at the Positions page directly.

Example 2 — grounded answer plus a governed action proposal
User: Lower my daily loss limit to 6000 for protection.
Agent: Sure — you're at a 10k daily loss limit and already 3.2k in the red today, so dropping to 6k leaves you about 2.8k of room before the kill switch trips. Reasonable if you want a tighter leash today. I can't flip the setting myself, but here's the change for you to approve:
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
            "temperature": 0.5,
            # gemini-2.5-flash is a THINKING model: hidden thought tokens are
            # charged against maxOutputTokens. At 1200 with 8 tool results the
            # thoughts ate the budget and the visible answer got cut off
            # mid-sentence (MAX_TOKENS). Give the answer real headroom AND cap
            # thinking so it can't starve the reply.
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget": 384},
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


def classify_playbook_by_query(query: str) -> List[str]:
    q = query.lower()
    
    playbook_tools = {
        "live-readiness": ["get_live_readiness", "get_feed_status", "get_token_status", "get_upstox_status"],
        "why-no-trade": [
            "get_skipped_signals", "get_market_data_status", "get_active_strategies", 
            "get_logs_errors", "get_today_orders", "get_today_fills", "get_recent_alerts",
            "get_historical_context"
        ],
        "strategy-loss-review": ["get_strategy_scorecard", "get_daily_report", "get_risk_snapshot", "get_strategy_score_explained", "get_historical_context"],
        "feed-token-diagnosis": ["get_upstox_status", "get_market_data_status", "get_feed_status", "get_token_status"],
        "eod-report": ["get_daily_report", "get_risk_snapshot", "get_today_fills", "get_today_orders", "get_historical_context"],
        "backtest-review": ["get_backtest_summary", "get_hermes_oos_validation", "get_edge_lab_snapshot", "get_active_strategies"],
        "edge-lab": ["get_edge_lab_snapshot", "get_hermes_oos_validation", "get_active_strategies"],
        "vps-deploy-check": ["get_live_readiness", "get_logs_errors", "get_recent_alerts"],
        "incident-postmortem": ["get_recent_alerts", "get_logs_errors", "get_today_fills", "get_core_events", "get_agent_tool_audit", "get_historical_context"],
    }
    
    wiki_keywords = ["wiki", "document", "documentation", "rule", "rules", "policy", "guideline", "decisions", "transcripts"]
    strategy_words = ["score", "grade", "performance", "sharpe", "sortino", "expectancy"]
    
    matched_tools = set()
    has_matches = False
    
    if any(w in q for w in strategy_words):
        matched_tools.update(playbook_tools["strategy-loss-review"])
        has_matches = True

    # "which strategy should I improve / work on / make better / optimize / fix"
    # — an improvement question. The DISCIPLINED answer (CLAUDE.md §13) comes from
    # OOS/Edge-Lab verdicts + scorecard, not vibes: name the strategy, cite its
    # grade/expectancy/OOS verdict, and don't advise tuning a NO_EDGE strategy.
    if any(w in q for w in ["improve", "make it better", "make them better", "better",
                            "work on", "work to", "optimize", "optimise", "fix",
                            "enhance", "strengthen", "which strategy", "what strategy",
                            "best strategy", "worst strategy", "underperform"]):
        matched_tools.update(playbook_tools["strategy-loss-review"])
        matched_tools.update(playbook_tools["edge-lab"])
        matched_tools.add("get_active_strategies")
        matched_tools.add("get_trade_attribution")
        matched_tools.add("get_research_hypotheses")
        has_matches = True

    if any(w in q for w in ["readiness", "ready", "pre-flight", "live readiness", "live ready", "oauth"]):
        matched_tools.update(playbook_tools["live-readiness"])
        has_matches = True
        
    if any(w in q for w in ["why", "no trade", "no trades", "not trade", "not trading", "no fills", "no entry", "skipped", "filtered", "rejected", "drought"]):
        matched_tools.update(playbook_tools["why-no-trade"])
        has_matches = True
        
    if any(w in q for w in ["feed", "token", "websocket", "tick", "ticks", "stale", "stalled", "connection", "disconnect"]):
        matched_tools.update(playbook_tools["feed-token-diagnosis"])
        has_matches = True
        
    if any(w in q for w in ["eod", "daily report", "summary", "end of day", "today pnl", "pnl today", "session close", "fills"]):
        matched_tools.update(playbook_tools["eod-report"])
        has_matches = True
        
    if any(w in q for w in ["backtest", "backtests", "historical", "ohlc", "simulation"]):
        matched_tools.update(playbook_tools["backtest-review"])
        has_matches = True

    # EOD held-to-theta OOS / Edge Lab (CLAUDE.md §13): "does X have an edge?",
    # base rates, walk-forward verdicts, config sweeps, data coverage.
    if any(w in q for w in ["edge", "oos", "out-of-sample", "out of sample", "walk-forward",
                            "walk forward", "candidate", "verdict", "no edge", "fragile",
                            "edge lab", "base rate", "base-rate", "short vol", "short-vol",
                            "coverage", "expectancy", "held to expiry", "hold to expiry"]):
        matched_tools.update(playbook_tools["edge-lab"])
        matched_tools.add("get_research_hypotheses")
        matched_tools.add("get_research_critique")
        has_matches = True

    # IMD intraday 1-min OOS (CLAUDE.md §14): option buyers / scalps QG-O5..O10.
    if any(w in q for w in ["intraday oos", "1-min", "1 min", "one minute", "minute oos",
                            "imd", "scalp", "scalper", "opening range", "orb", "buyer", "buyers"]):
        matched_tools.add("get_intraday_oos")
        has_matches = True
        
    # RAE regime ensemble (CLAUDE.md §18): what regime we're in, who's active vs
    # standing down, per-regime P&L, router enforcement state.
    if any(w in q for w in ["regime", "ensemble", "rae", "stand down", "stand-down",
                            "standing down", "trend day", "range day", "chop", "router",
                            "which strategy is active", "specialist"]):
        matched_tools.add("get_regime_status")
        has_matches = True

    # EdgeMath sizing advice (CLAUDE.md §16): edge-based size, risk multiplier, ratchet.
    if any(w in q for w in ["edgemath", "edge math", "sizing", "size up", "size down",
                            "risk multiplier", "kelly", "position size", "ratchet"]):
        matched_tools.add("get_edge_math_advice")
        has_matches = True

    if any(w in q for w in ["vps", "deploy", "deployment", "container", "docker", "health", "restart"]):
        matched_tools.update(playbook_tools["vps-deploy-check"])
        has_matches = True
        
    if any(w in q for w in ["incident", "postmortem", "outage", "timeline", "crash", "post-mortem"]):
        matched_tools.update(playbook_tools["incident-postmortem"])
        has_matches = True
        
    if any(w in q for w in wiki_keywords):
        matched_tools.add("search_wiki")
        has_matches = True

    # Knowledge-map / graph "view" questions: how notes interconnect, hubs, orphans.
    if any(w in q for w in ["map", "graph", "knowledge map", "connected", "connections",
                            "link graph", "related notes", "backlinks", "orphan", "structure of notes"]):
        matched_tools.add("get_knowledge_graph")
        matched_tools.add("search_wiki")
        has_matches = True
        
    if any(w in q for w in ["history", "historical", "past", "yesterday", "days", "weeks", "month", "previous", "context"]):
        matched_tools.add("get_historical_context")
        matched_tools.add("recall_memory")
        has_matches = True

    # HSB-06: external macro/news/event-risk context via Gemini Google-Search grounding.
    # Bounded to clearly external questions so it never fires on internal-data queries.
    if any(w in q for w in [
        "news", "event", "macro", "rbi", "fed", "policy", "budget", "global",
        "sgx", "gift nifty", "event risk", "results", "earnings", "expiry",
        "holiday", "market today", "happening", "world", "geopolit", "crude", "vix news"
    ]):
        matched_tools.add("get_external_context")
        has_matches = True
        
    # HSI-15: trade-attribution "why" layer — fires on causal/diagnostic questions
    # about wins/losses, regime/bias/structure/exit performance.
    if any(w in q for w in [
        "why", "attribution", "lose", "lost", "losing", "win rate", "winning",
        "expectancy", "r multiple", "r-multiple", "which regime", "best structure",
        "worst", "which strategy", "exit reason", "hold time", "performed", "performance by",
    ]):
        matched_tools.add("get_trade_attribution")
        has_matches = True

    # HSI-34: brain-health / self-improvement meta questions.
    if any(w in q for w in [
        "brain health", "brain-health", "lesson", "lessons", "learning", "getting smarter",
        "self-improve", "self improve", "self-improvement", "what have you learned",
        "what has hermes learned", "confidence", "decayed",
    ]):
        matched_tools.add("get_hermes_brain_health")
        has_matches = True

    if any(w in q for w in [
        "hypothesis", "hypotheses", "research ledger", "research desk", "prove",
        "proven", "unproven", "falsify", "falsification", "critic", "critique",
        "verifier", "verification", "what is missing", "missing data",
    ]):
        matched_tools.add("get_research_hypotheses")
        matched_tools.add("get_research_critique")
        matched_tools.add("get_research_desk")
        has_matches = True

    if not has_matches:
        matched_tools.update([
            "get_risk_snapshot",
            "get_open_positions",
            "get_active_strategies",
            "get_today_orders",
            "get_recent_alerts",
        ])
        
    # Always include baseline/essential state tools:
    matched_tools.update([
        "get_risk_snapshot",
        "get_open_positions",
        "get_active_strategies",
        "recall_memory",
        # Obsidian second-brain grounding: search_wiki live-syncs the Obsidian
        # vault from disk (sync_wiki_directory) and surfaces notes relevant to
        # the message. Always-on so every Hermes turn is grounded in the
        # knowledge base, not only when the user types an explicit wiki keyword.
        "search_wiki",
    ])
    
    return [t for t in READ_ONLY_AGENT_TOOLS if t in matched_tools]


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

    # Tool selection: the keyword router is the always-on safety net + baseline
    # grounding; the Gemini function-calling planner adds any tool the keyword
    # matcher missed for off-script phrasings. Union, dedup in READ_ONLY order,
    # cap for cost. Fails open to keyword-only when the planner returns [].
    keyword_tools = classify_playbook_by_query(content)
    planned_tools = await _plan_tools_via_gemini(content, recent_messages)
    _merged = set(keyword_tools) | set(planned_tools)
    max_tools = int(os.environ.get("HERMES_MAX_TOOLS_PER_TURN", "18"))
    active_tools = [t for t in READ_ONLY_AGENT_TOOLS if t in _merged][:max_tools]
    tool_selection = {
        "keyword": keyword_tools,
        "planned": planned_tools,
        "active": active_tools,
        "planner_used": bool(planned_tools),
    }
    tool_results = await asyncio.gather(*[_run_agent_tool(name, user, query=content) for name in active_tools])
    reply = await _gemini_agent_reply(content, list(tool_results), recent_messages)
    
    # Parse and store proposed action
    reply, pending_action_doc = _parse_and_store_pending_action(reply, user["id"])
    if pending_action_doc:
        await db.pending_actions.insert_one(pending_action_doc)

    # HSB-08: parse and store a testable recommendation (self-improvement loop input)
    reply, recommendation_doc = _parse_and_store_recommendation(reply, user["id"])
    if recommendation_doc:
        try:
            await db.hermes_recommendations.insert_one(recommendation_doc)
        except Exception as rec_exc:
            logger.warning("Failed to store recommendation: %s", rec_exc)

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
        "tool_selection": tool_selection,
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
    
    allowed_actions = {"update_profile", "draft_wiki_note", "draft_task_entry", "draft_incident_report", "draft_pr_summary", "draft_strategy_pause", "draft_config_change"}
    if action_type not in allowed_actions:
        raise HTTPException(status_code=400, detail=f"Unsupported action type: {action_type}")

    # Safeguard assert to verify no execution of dangerous operations.
    # NOTE: pausing a strategy is a non-trade status flip (stops new entries); it is NOT
    # an order/trade action, so it stays within Hermes' safety bounds.
    for keyword in ("order", "trade", "buy", "sell", "cancel", "broker", "live_enabled", "keys"):
        if keyword in action_type.lower():
            raise HTTPException(status_code=400, detail="Action type violates safety bounds")
            
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
            
    elif action_type == "draft_wiki_note":
        title = params.get("title")
        body_markdown = params.get("body_markdown")
        folder = params.get("folder", "General").strip()
        
        if not title or not body_markdown:
            raise HTTPException(status_code=400, detail="title and body_markdown are required")
            
        allowed_folders = {"YouTube transcripts", "Meeting transcripts", "Decisions", "Projects", "Trading Rules", "General"}
        if folder not in allowed_folders:
            raise HTTPException(status_code=400, detail=f"Invalid folder. Must be one of {allowed_folders}")
            
        import re
        from datetime import timedelta as _timedelta
        # Auto-dedupe: a memory/wiki note must never fail to save just because a note
        # with the same title already exists (two EOD "Session Memory <date>" notes,
        # repeated AI drafts, etc.). Append an IST time suffix so each is uniquely
        # saveable instead of hard-rejecting. (2026-06-30)
        exists = await db.wiki_docs.find_one({"user_id": user["id"], "title": title})
        if exists:
            _suffix = (datetime.now(timezone.utc) + _timedelta(hours=5, minutes=30)).strftime("%H:%M:%S")
            title = f"{title} ({_suffix} IST)"
            if await db.wiki_docs.find_one({"user_id": user["id"], "title": title}):
                title = f"{title} {uuid.uuid4().hex[:4]}"

        slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
        if not slug:
            slug = str(uuid.uuid4())[:8]
        # The slug is the wiki_doc id (primary key) — guarantee it is unique too.
        if await db.wiki_docs.find_one({"id": slug}):
            slug = f"{slug}-{uuid.uuid4().hex[:6]}"

        now_str = datetime.now(timezone.utc).isoformat()
        from routes.wiki import parse_markdown_links, save_wiki_to_disk, rebuild_all_backlinks
        links = parse_markdown_links(body_markdown)
        
        doc = {
            "id": slug,
            "title": title,
            "topic": folder,
            "content": body_markdown,
            "tags": ["hermes-draft"],
            "links": links,
            "backlinks": [],
            "metadata": {"source": "hermes-agent"},
            "user_id": user["id"],
            "created_at": now_str,
            "updated_at": now_str,
        }
        
        await db.wiki_docs.insert_one(doc)
        try:
            save_wiki_to_disk(title, folder, body_markdown, ["hermes-draft"], {"source": "hermes-agent"})
        except Exception as exc:
            logger.error("Failed to write approved wiki note to disk: %s", exc)
        await rebuild_all_backlinks(user["id"])
        
    elif action_type == "draft_task_entry":
        task_id = params.get("task_id")
        title = params.get("title")
        body_markdown = params.get("body_markdown")
        
        if not task_id or not title or not body_markdown:
            raise HTTPException(status_code=400, detail="task_id, title, and body_markdown are required")
            
        root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        tasks_file_path = os.path.join(root_dir, "TASKS.md")
        try:
            formatted_entry = f"\n\n### {task_id} — {title}\n- **Status**: `[ ]`\n- **Tier**: 2\n- **Description**: {body_markdown}\n"
            with open(tasks_file_path, "a", encoding="utf-8") as f:
                f.write(formatted_entry)
        except Exception as exc:
            logger.error("Failed to append draft task entry to TASKS.md: %s", exc)
            raise HTTPException(status_code=500, detail=f"Failed to write to TASKS.md: {exc}")
            
    elif action_type == "draft_incident_report":
        title = params.get("title")
        body_markdown = params.get("body_markdown")
        
        if not title or not body_markdown:
            raise HTTPException(status_code=400, detail="title and body_markdown are required")
            
        import re
        slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
        if not slug:
            slug = str(uuid.uuid4())[:8]
            
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename_title = f"{date_str}-{slug}"
        
        now_str = datetime.now(timezone.utc).isoformat()
        from routes.wiki import parse_markdown_links, save_wiki_to_disk, rebuild_all_backlinks
        links = parse_markdown_links(body_markdown)
        
        doc = {
            "id": filename_title,
            "title": f"Incident: {title}",
            "topic": "Incidents",
            "content": body_markdown,
            "tags": ["incident", "hermes-draft"],
            "links": links,
            "backlinks": [],
            "metadata": {"source": "hermes-agent"},
            "user_id": user["id"],
            "created_at": now_str,
            "updated_at": now_str,
        }
        
        await db.wiki_docs.insert_one(doc)
        try:
            save_wiki_to_disk(filename_title, "Incidents", body_markdown, ["incident", "hermes-draft"], {"source": "hermes-agent", "incident_title": title})
        except Exception as exc:
            logger.error("Failed to write incident report to disk: %s", exc)
        await rebuild_all_backlinks(user["id"])
        
    elif action_type == "draft_pr_summary":
        title = params.get("title")
        body_markdown = params.get("body_markdown")

        if not title or not body_markdown:
            raise HTTPException(status_code=400, detail="title and body_markdown are required")

    elif action_type == "draft_strategy_pause":
        # HSB-10: approval-gated, non-trade status flip — pauses a strategy so it stops
        # emitting new entries (e.g. on a loss streak). Never closes/places trades.
        strategy_id = params.get("strategy_id")
        reason = (params.get("reason") or "Paused by Hermes (founder-approved)").strip()
        if not strategy_id:
            raise HTTPException(status_code=400, detail="strategy_id is required")

        strat = await db.strategies.find_one({"id": strategy_id, "user_id": user["id"]})
        if not strat:
            raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found for this account")

        await db.strategies.update_one(
            {"id": strategy_id, "user_id": user["id"]},
            {"$set": {
                "status": "paused",
                "paused_reason": reason,
                "paused_by": "hermes-agent",
                "paused_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
        )
        # Mark any open recommendation that advised pausing this strategy as acted-on,
        # so the HSB-09 scorer knows the advice was followed.
        try:
            await db.hermes_recommendations.update_many(
                {"user_id": user["id"], "strategy_id": strategy_id, "status": "open"},
                {"$set": {"acted_on": True}},
            )
        except Exception as rec_exc:
            logger.warning("Failed to mark recommendation acted_on for pause: %s", rec_exc)

    elif action_type == "draft_config_change":
        # HSI-51: OOS-gated, approval-gated config edit. Non-trade. Only a lesson with
        # oos_passed=true can drive it; only DB-durable fields are applied (others get
        # re-synced away → surfaced as an edit-in-template task); rate-limited + reversible.
        from core import hermes_advisor as _adv
        strategy_id = params.get("strategy_id")
        field = params.get("field")
        proposed = params.get("proposed")
        lesson_id = params.get("lesson_id")
        if not (strategy_id and field and lesson_id) or proposed is None:
            raise HTTPException(status_code=400, detail="strategy_id, field, proposed, lesson_id are required")

        lesson = await db.hermes_lessons.find_one({"lesson_id": lesson_id, "user_id": user["id"]})
        if not lesson:
            raise HTTPException(status_code=404, detail=f"Lesson {lesson_id} not found")
        if not lesson.get("oos_passed"):
            raise HTTPException(status_code=400, detail="Lesson has not passed OOS validation — cannot apply (judge-first).")

        recent = await _adv.count_recent_changes(db, user["id"])
        if not _adv.within_rate_limit(recent):
            raise HTTPException(status_code=429, detail=f"Config-change rate limit reached ({recent}/{_adv.HERMES_MAX_CHANGES_PER_WEEK} this week).")

        strat = await db.strategies.find_one({"id": strategy_id, "user_id": user["id"]})
        if not strat:
            raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

        if not _adv.is_db_durable_field(field):
            # Non-durable field: a DB edit is re-synced away on restart → don't fake it.
            # Surface an edit-in-template task instead (CLAUDE.md KEY MECHANIC).
            root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            try:
                with open(os.path.join(root_dir, "TASKS.md"), "a", encoding="utf-8") as f:
                    f.write(f"\n\n### HERMES-EDIT-IN-TEMPLATE — {strat.get('name')} `{field}` → {proposed}\n"
                            f"- **Status**: `[ ]`\n- Lesson {lesson_id} (oos_passed). {params.get('reason','')}\n"
                            f"- Field is NOT DB-durable — edit the in-code template in server.py, not the DB.\n")
            except Exception as exc:
                logger.error("Failed to append edit-in-template task: %s", exc)
            await db.pending_actions.update_one(
                {"action_id": req.action_id},
                {"$set": {"status": "approved", "outcome": "edit_in_template_task",
                          "executed_at": datetime.now(timezone.utc).isoformat()}})
            return {"status": "approved", "action_id": req.action_id, "outcome": "edit_in_template_task"}

        # DB-durable field: read prior value, apply, record for reversibility + HSI-53 tag.
        prior = strat
        for part in field.split("."):
            prior = (prior or {}).get(part) if isinstance(prior, dict) else None
        pre_rows = await db.trade_attribution.find(
            {"user_id": user["id"], "strategy_id": strategy_id}).to_list(500)
        pre_exp = round(sum(float(r.get("realized_pnl") or 0) for r in pre_rows) / len(pre_rows), 2) if pre_rows else 0.0

        await db.strategies.update_one(
            {"id": strategy_id, "user_id": user["id"]},
            {"$set": {field: proposed, "updated_at": datetime.now(timezone.utc).isoformat()}})
        await _adv.record_applied_change(db, user["id"], {
            "strategy_id": strategy_id, "strategy_name": strat.get("name"), "field": field,
            "prior_value": prior, "proposed_value": proposed, "lesson_id": lesson_id,
            "pre_expectancy": pre_exp, "reason": params.get("reason", ""),
            "oos_evidence": lesson.get("last_oos_result"),
        })
        try:
            await _adv.compile_hermes_advice(db, user["id"])
        except Exception as adv_exc:
            logger.warning("compile_hermes_advice failed post-apply: %s", adv_exc)

    await db.pending_actions.update_one(
        {"action_id": req.action_id},
        {"$set": {"status": "approved", "executed_at": datetime.now(timezone.utc).isoformat()}}
    )
    
    try:
        audit_entry = {
            "id": str(uuid.uuid4()),
            "name": f"approve_{action_type}",
            "user_id": user["id"],
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "args": {
                "action_id": req.action_id,
                "action_type": action_type,
                "params": params
            },
            "stale": False,
            "confidence": 1.0,
            "warnings": [],
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.agent_tool_audit.insert_one(audit_entry)
    except Exception as audit_exc:
        logger.error("Failed to write to agent_tool_audit in approve_agent_action: %s", audit_exc)
        
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
    from server import _market_score_for_strategy
    
    market_context = await _load_current_market_context(user)
    scores = [_market_score_for_strategy(row, market_context["by_symbol"]) for row in rows]
    for score in scores:
        score["user_id"] = user_id
        score["research_context"] = market_context["research_context"]
        await db.strategy_ai_scores.update_one(
            {"strategy_id": score["strategy_id"], "user_id": user_id},
            {"$set": score},
            upsert=True,
        )
    result = {
        "scores": [{k: v for k, v in score.items() if k != "user_id"} for score in scores],
        "provider": "gemini-context" if os.environ.get("GEMINI_API_KEY") else "local-market-structure",
        "research_context": market_context["research_context"],
        "retired_context": market_context["retired_context"],
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
    from server import _market_score_for_strategy, _google_ai_reply
    
    market_context = await _load_current_market_context(user)
    market_rows = market_context["rows"]
    scores = [_market_score_for_strategy(row, market_context["by_symbol"]) for row in strategies]
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
        "research_context": market_context["research_context"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/training-context")
async def ai_training_context(user=Depends(get_current_user)):
    strategies = await db.strategies.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).to_list(200)
    recent_scores = await db.strategy_ai_scores.find({"user_id": user["id"]}, {"_id": 0, "user_id": 0}).sort("generated_at", -1).to_list(100)
    
    # Runtime dynamic imports to avoid circular dependencies
    from server import _strategy_out
    market_context = await _load_current_market_context(user)
    
    return {
        "purpose": "Context-feed payload for Gemini prompts and offline fine-tuning experiments.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "research_context": market_context["research_context"],
        "market": market_context["rows"],
        "commodities": market_context["retired_context"]["commodities"]["rows"],
        "retired_context": market_context["retired_context"],
        "strategies": [_strategy_out(row).model_dump() for row in strategies],
        "recent_ai_scores": recent_scores,
    }
