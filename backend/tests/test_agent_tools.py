import pytest
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

# Ensure backend is in the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.ai import _run_agent_tool, READ_ONLY_AGENT_TOOLS

@pytest.mark.anyio
async def test_run_agent_tool_envelope_fields():
    """Verify that _run_agent_tool returns the extended metadata envelope."""
    mock_db = MagicMock()
    
    # Mock database queries for get_orders
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(return_value=[
        {"id": "ord-1", "symbol": "NIFTY", "side": "BUY", "created_at": "2026-06-20T00:00:00"}
    ])
    mock_db.orders.find.return_value = mock_cursor
    mock_db.agent_tool_audit.insert_one = AsyncMock()

    user = {"id": "test-trader-1"}

    with patch("routes.ai.db", mock_db):
        res = await _run_agent_tool("get_orders", user)
        
    assert res["status"] == "ok"
    assert res["name"] == "get_orders"
    assert res["source"] == "db.orders"
    assert res["stale"] is False
    assert res["confidence"] == 1.0
    assert isinstance(res["warnings"], list)
    assert res["user"] == "test-trader-1"
    assert res["account"] == "test-trader-1"
    assert "timestamp" in res
    assert "started_at" in res
    assert "finished_at" in res
    assert len(res["data"]) == 1
    assert res["data"][0]["id"] == "ord-1"
    
    # Verify audit collection insert was called
    mock_db.agent_tool_audit.insert_one.assert_called_once()


@pytest.mark.anyio
async def test_run_agent_tool_today_fills():
    """Verify that get_today_fills correctly queries trade_fills and returns the envelope."""
    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(return_value=[
        {"id": "fill-1", "realized_pnl": 1500.0, "created_at": "2026-06-20T09:30:00"}
    ])
    mock_db.trade_fills.find.return_value = mock_cursor
    mock_db.agent_tool_audit.insert_one = AsyncMock()

    user = {"id": "test-trader-1"}

    with patch("routes.ai.db", mock_db), patch("server.get_trading_day_window_ist", return_value=("2026-06-20T00:00:00", "2026-06-20T23:59:59")):
        res = await _run_agent_tool("get_today_fills", user)

    assert res["status"] == "ok"
    assert res["source"] == "db.trade_fills"
    assert len(res["data"]) == 1
    assert res["data"][0]["realized_pnl"] == 1500.0


@pytest.mark.anyio
async def test_run_agent_tool_skipped_signals():
    """Verify that get_skipped_signals queries both signals and skipped_signals and returns the merged data."""
    mock_db = MagicMock()
    
    mock_cursor_signals = MagicMock()
    mock_cursor_signals.sort.return_value = mock_cursor_signals
    mock_cursor_signals.to_list = AsyncMock(return_value=[
        {"id": "sig-1", "status": "FILTERED", "rejection_reason": "Theta check failed"}
    ])
    mock_db.signals.find.return_value = mock_cursor_signals

    mock_cursor_skipped = MagicMock()
    mock_cursor_skipped.sort.return_value = mock_cursor_skipped
    mock_cursor_skipped.to_list = AsyncMock(return_value=[
        {"id": "skip-1", "strategy_id": "strat-1", "count": 5}
    ])
    mock_db.skipped_signals.find.return_value = mock_cursor_skipped

    mock_db.agent_tool_audit.insert_one = AsyncMock()

    user = {"id": "test-trader-1"}

    with patch("routes.ai.db", mock_db):
        res = await _run_agent_tool("get_skipped_signals", user)

    assert res["status"] == "ok"
    assert res["source"] == "db.signals / db.skipped_signals"
    assert "signals_skipped" in res["data"]
    assert "aggregated_skipped_signals" in res["data"]
    assert res["data"]["signals_skipped"][0]["id"] == "sig-1"
    assert res["data"]["aggregated_skipped_signals"][0]["id"] == "skip-1"


@pytest.mark.anyio
async def test_run_agent_tool_search_wiki():
    """Verify search_wiki queries db.wiki_docs with appropriate regex filter."""
    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.to_list = AsyncMock(return_value=[
        {"title": "Risk Management Guide", "topic": "Trading Rules"}
    ])
    mock_db.wiki_docs.find.return_value = mock_cursor
    mock_db.agent_tool_audit.insert_one = AsyncMock()

    user = {"id": "test-trader-1"}

    with patch("routes.ai.db", mock_db), patch("routes.wiki.sync_wiki_directory", AsyncMock()) as mock_sync:
        res = await _run_agent_tool("search_wiki", user, query="risk management")
        mock_sync.assert_called_once_with(user=user)

    assert res["status"] == "ok"
    assert res["source"] == "db.wiki_docs"
    assert len(res["data"]) == 1
    assert res["data"][0]["title"] == "Risk Management Guide"
    
    # Assert query filter was passed to db.wiki_docs.find
    args = mock_db.wiki_docs.find.call_args[0][0]
    assert args["user_id"] == "test-trader-1"
    assert "$and" in args


@pytest.mark.anyio
async def test_run_agent_tool_warnings_on_not_ready():
    """Verify that get_live_readiness populates warnings when the status is NOT_READY."""
    mock_db = MagicMock()
    mock_db.agent_tool_audit.insert_one = AsyncMock()
    
    mock_readiness_response = {
        "status": "NOT_READY",
        "reasons": ["Upstox market data feed not connected", "System not armed"]
    }
    
    user = {"id": "test-trader-1"}

    with patch("routes.ai.db", mock_db), patch("routes.ops.ops_live_readiness", AsyncMock(return_value=mock_readiness_response)):
        res = await _run_agent_tool("get_live_readiness", user)
        
    assert res["status"] == "ok"
    assert res["source"] == "routes.ops.ops_live_readiness"
    assert res["confidence"] == 0.5
    assert "Upstox market data feed not connected" in res["warnings"]
    assert "System not armed" in res["warnings"]


@pytest.mark.anyio
async def test_agent_chat_api_response():
    """Verify that agent_chat API endpoint returns and stores full tools_used envelope metrics."""
    from routes.ai import agent_chat, ChatReq
    
    mock_db = MagicMock()
    mock_db.ai_chats.insert_many = AsyncMock()
    mock_db.agent_audit_logs.insert_one = AsyncMock()
    
    # Mock recent chat history query
    mock_cursor_history = MagicMock()
    mock_cursor_history.sort.return_value = mock_cursor_history
    mock_cursor_history.to_list = AsyncMock(return_value=[])
    mock_db.ai_chats.find.return_value = mock_cursor_history

    # Mock tool results for a simple tool run inside agent_chat
    # We mock _run_agent_tool directly to return a single mock envelope
    mock_envelope = {
        "name": "get_orders",
        "status": "ok",
        "source": "db.orders",
        "stale": False,
        "confidence": 1.0,
        "warnings": ["Low balance warning"],
        "user": "test-trader-1",
        "account": "test-trader-1",
        "timestamp": "2026-06-20T00:30:00",
        "started_at": "2026-06-20T00:29:59",
        "finished_at": "2026-06-20T00:30:00",
        "data": []
    }

    req = ChatReq(session_id="session-xyz", message="show orders")
    user = {"id": "test-trader-1"}

    with patch("routes.ai.db", mock_db), \
         patch("routes.ai.READ_ONLY_AGENT_TOOLS", ["get_orders"]), \
         patch("routes.ai.classify_playbook_by_query", return_value=["get_orders"]), \
         patch("routes.ai._run_agent_tool", AsyncMock(return_value=mock_envelope)), \
         patch("routes.ai._gemini_agent_reply", AsyncMock(return_value="Here is your info.")):
             
        res = await agent_chat(req, user)

    # Verify returned JSON response
    assert res["role"] == "assistant"
    assert res["content"] == "Here is your info."
    assert len(res["tools_used"]) == 1
    tool_info = res["tools_used"][0]
    assert tool_info["name"] == "get_orders"
    assert tool_info["source"] == "db.orders"
    assert tool_info["stale"] is False
    assert tool_info["confidence"] == 1.0
    assert tool_info["warnings"] == ["Low balance warning"]
    assert tool_info["timestamp"] == "2026-06-20T00:30:00"

    # Verify database insert of bot_msg carries the tools_used values
    inserted_args = mock_db.ai_chats.insert_many.call_args[0][0]
    assert len(inserted_args) == 2  # user_msg and bot_msg
    bot_msg_inserted = inserted_args[1]
    assert bot_msg_inserted["role"] == "assistant"
    assert "tools_used" in bot_msg_inserted
    assert bot_msg_inserted["tools_used"][0]["source"] == "db.orders"


@pytest.mark.anyio
async def test_run_agent_tool_get_backtest_summary():
    """Verify get_backtest_summary extracts strategy ID and dates from query and passes to ops_options_backtest."""
    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.to_list = AsyncMock(return_value=[
        {"id": "strat_buyer_opt"}
    ])
    mock_db.strategies.find.return_value = mock_cursor
    mock_db.agent_tool_audit.insert_one = AsyncMock()

    user = {"id": "test-trader-1"}
    mock_backtest_fn = AsyncMock(return_value={"status": "success", "results": []})

    with patch("routes.ai.db", mock_db), \
         patch("routes.ops.ops_options_backtest", mock_backtest_fn):
        res = await _run_agent_tool(
            "get_backtest_summary", 
            user, 
            query="Run backtest for strat_buyer_opt from 2026-06-01 to 2026-06-15"
        )

    assert res["status"] == "ok"
    assert res["source"] == "routes.ops.ops_options_backtest"
    mock_backtest_fn.assert_called_once_with(
        strategy_id="strat_buyer_opt",
        start="2026-06-01",
        end="2026-06-15",
        user=user
    )


@pytest.mark.anyio
async def test_get_pending_actions():
    """Verify that get_pending_actions queries db.pending_actions and returns pending items."""
    from routes.ai import get_pending_actions
    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(return_value=[
        {"action_id": "act-1", "action_type": "draft_wiki_note", "status": "pending"}
    ])
    mock_db.pending_actions.find.return_value = mock_cursor
    
    user = {"id": "test-trader-1"}
    
    with patch("routes.ai.db", mock_db):
        res = await get_pending_actions(user=user)
        
    assert len(res) == 1
    assert res[0]["action_id"] == "act-1"
    mock_db.pending_actions.find.assert_called_once_with(
        {"user_id": "test-trader-1", "status": "pending"},
        {"_id": 0}
    )


@pytest.mark.anyio
async def test_approve_wiki_note_action():
    """Verify that approving a draft_wiki_note writes to disk and db.wiki_docs."""
    from routes.ai import approve_agent_action, ActionDecisionReq
    mock_db = MagicMock()
    mock_db.pending_actions.find_one = AsyncMock(return_value={
        "action_id": "act-123",
        "action_type": "draft_wiki_note",
        "user_id": "test-trader-1",
        "status": "pending",
        "params": {
            "title": "Hermes Rules",
            "body_markdown": "Hermes guidelines details",
            "folder": "Projects"
        }
    })
    mock_db.wiki_docs.find_one = AsyncMock(return_value=None)
    mock_db.wiki_docs.insert_one = AsyncMock()
    mock_db.pending_actions.update_one = AsyncMock()
    mock_db.agent_tool_audit.insert_one = AsyncMock()
    
    user = {"id": "test-trader-1"}
    req = ActionDecisionReq(action_id="act-123")
    
    mock_save_disk = MagicMock()
    mock_rebuild_links = AsyncMock()
    
    with patch("routes.ai.db", mock_db), \
         patch("routes.wiki.save_wiki_to_disk", mock_save_disk), \
         patch("routes.wiki.rebuild_all_backlinks", mock_rebuild_links):
        res = await approve_agent_action(req, user=user)
        
    assert res["status"] == "approved"
    mock_db.wiki_docs.insert_one.assert_called_once()
    doc_inserted = mock_db.wiki_docs.insert_one.call_args[0][0]
    assert doc_inserted["title"] == "Hermes Rules"
    assert doc_inserted["topic"] == "Projects"
    
    mock_save_disk.assert_called_once_with(
        "Hermes Rules", "Projects", "Hermes guidelines details", ["hermes-draft"], {"source": "hermes-agent"}
    )
    mock_rebuild_links.assert_called_once_with("test-trader-1")


@pytest.mark.anyio
async def test_approve_task_entry_action():
    """Verify that approving draft_task_entry appends to TASKS.md."""
    from routes.ai import approve_agent_action, ActionDecisionReq
    mock_db = MagicMock()
    mock_db.pending_actions.find_one = AsyncMock(return_value={
        "action_id": "act-456",
        "action_type": "draft_task_entry",
        "user_id": "test-trader-1",
        "status": "pending",
        "params": {
            "task_id": "TASK-H888",
            "title": "Hermes task",
            "body_markdown": "Test instructions"
        }
    })
    mock_db.pending_actions.update_one = AsyncMock()
    mock_db.agent_tool_audit.insert_one = AsyncMock()
    
    user = {"id": "test-trader-1"}
    req = ActionDecisionReq(action_id="act-456")
    
    mock_open = MagicMock()
    
    with patch("routes.ai.db", mock_db), \
         patch("builtins.open", mock_open):
        res = await approve_agent_action(req, user=user)
        
    assert res["status"] == "approved"
    mock_open.assert_called_once()
    assert mock_open.call_args[0][1] == "a"


@pytest.mark.anyio
async def test_run_agent_tool_get_core_events():
    """Verify that get_core_events queries db.core_events and returns events list."""
    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(return_value=[
        {"event_id": "evt-123", "event_type": "STRATEGY_SIGNAL", "created_at": "2026-06-20T10:00:00Z"}
    ])
    mock_db.core_events.find.return_value = mock_cursor
    mock_db.agent_tool_audit.insert_one = AsyncMock()

    user = {"id": "test-trader-1"}

    with patch("routes.ai.db", mock_db):
        res = await _run_agent_tool("get_core_events", user)

    assert res["status"] == "ok"
    assert res["source"] == "db.core_events"
    assert len(res["data"]) == 1
    assert res["data"][0]["event_id"] == "evt-123"
    mock_db.core_events.find.assert_called_once_with(
        {"user_id": "test-trader-1"},
        {"_id": 0}
    )


@pytest.mark.anyio
async def test_run_agent_tool_get_agent_tool_audit():
    """Verify that get_agent_tool_audit queries db.agent_tool_audit and returns audit records."""
    mock_db = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(return_value=[
        {"id": "audit-456", "name": "get_orders", "status": "ok", "created_at": "2026-06-20T10:05:00Z"}
    ])
    mock_db.agent_tool_audit.find.return_value = mock_cursor
    mock_db.agent_tool_audit.insert_one = AsyncMock()

    user = {"id": "test-trader-1"}

    with patch("routes.ai.db", mock_db):
        res = await _run_agent_tool("get_agent_tool_audit", user)

    assert res["status"] == "ok"
    assert res["source"] == "db.agent_tool_audit"
    assert len(res["data"]) == 1
    assert res["data"][0]["id"] == "audit-456"
    mock_db.agent_tool_audit.find.assert_called_once_with(
        {"user_id": "test-trader-1"},
        {"_id": 0}
    )


@pytest.mark.anyio
async def test_approve_incident_report_action():
    """Verify that approving a draft_incident_report writes to disk, db.wiki_docs, and updates action status."""
    from routes.ai import approve_agent_action, ActionDecisionReq
    mock_db = MagicMock()
    mock_db.pending_actions.find_one = AsyncMock(return_value={
        "action_id": "act-789",
        "action_type": "draft_incident_report",
        "user_id": "test-trader-1",
        "status": "pending",
        "params": {
            "title": "Outage feed stall",
            "body_markdown": "Details about the stall"
        }
    })
    mock_db.wiki_docs.insert_one = AsyncMock()
    mock_db.pending_actions.update_one = AsyncMock()
    mock_db.agent_tool_audit.insert_one = AsyncMock()
    
    user = {"id": "test-trader-1"}
    req = ActionDecisionReq(action_id="act-789")
    
    mock_save_disk = MagicMock()
    mock_rebuild_links = AsyncMock()
    
    with patch("routes.ai.db", mock_db), \
         patch("routes.wiki.save_wiki_to_disk", mock_save_disk), \
         patch("routes.wiki.rebuild_all_backlinks", mock_rebuild_links):
        res = await approve_agent_action(req, user=user)
        
    assert res["status"] == "approved"
    mock_db.wiki_docs.insert_one.assert_called_once()
    doc_inserted = mock_db.wiki_docs.insert_one.call_args[0][0]
    assert doc_inserted["title"] == "Incident: Outage feed stall"
    assert doc_inserted["topic"] == "Incidents"
    
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    expected_filename = f"{current_date}-outage-feed-stall"
    mock_save_disk.assert_called_once_with(
        expected_filename, "Incidents", "Details about the stall", ["incident", "hermes-draft"], {"source": "hermes-agent", "incident_title": "Outage feed stall"}
    )
    mock_rebuild_links.assert_called_once_with("test-trader-1")


@pytest.mark.anyio
async def test_run_agent_tool_get_strategy_score_explained():
    """Verify that get_strategy_score_explained fetches strategy details, computes metrics, and sets proper confidence."""
    mock_db = MagicMock()
    mock_db.agent_tool_audit.insert_one = AsyncMock()
    
    # Mock strategies collection find_one method
    mock_db.strategies.find_one = AsyncMock(return_value={
        "id": "strat-1",
        "name": "NIFTY EMA Scalper",
        "strategy_type": "Option Buying",
        "status": "live",
        "visual_config": {"options": {"structure": "single_leg"}}
    })
    
    # Mock strategies collection find method for query parsing
    mock_cursor_find = MagicMock()
    mock_cursor_find.to_list = AsyncMock(return_value=[{"id": "strat-1"}])
    mock_db.strategies.find.return_value = mock_cursor_find
    
    # Mock build_scorecard response
    mock_scorecard_rows = [
        {
            "strategy_id": "strat-1",
            "name": "NIFTY EMA Scalper",
            "structure": "single_leg",
            "strategy_type": "Option Buying",
            "status": "live",
            "grade": "A",
            "total_trades": 8, # < 10 threshold
            "total_pnl": 5000.0,
            "wins": 6,
            "losses": 2,
            "win_rate": 0.75,
            "expectancy": 625.0,
            "sharpe": 1.8,
            "sortino": 2.1
        }
    ]
    
    # Mock watchlist and _market_score_for_strategy
    mock_watchlist = AsyncMock(return_value=[{"symbol": "NIFTY", "price": 24000.0, "pct": 0.5, "source": "test"}])
    mock_market_score = MagicMock(return_value={"score": 85.0, "reason": "test fit"})
    
    user = {"id": "test-trader-1"}
    
    with patch("routes.ai.db", mock_db), \
         patch("core.strategy_scorecard.build_scorecard", AsyncMock(return_value=mock_scorecard_rows)), \
         patch("routes.market.watchlist", mock_watchlist), \
         patch("routes.market.commodity_watchlist", AsyncMock(return_value=[])), \
         patch("server._market_score_for_strategy", mock_market_score):
             
        # Fails without strategy_id resolved
        res_fail = await _run_agent_tool("get_strategy_score_explained", user, query="Show score")
        assert "error" in res_fail["data"]
        
        # Succeeds with strategy_id
        res = await _run_agent_tool("get_strategy_score_explained", user, query="what is the score of strat-1")
        
    assert res["status"] == "ok"
    assert res["source"] == "db.strategies / core.strategy_scorecard"
    assert res["confidence"] == 0.3  # < 10 trades => 0.3 confidence
    assert any("provisional" in w for w in res["warnings"])
    assert res["data"]["strategy_id"] == "strat-1"
    assert res["data"]["grade"] == "A"
    assert res["data"]["regime_fit"]["score"] == 85.0


def test_classify_playbook_by_query():
    """Verify that classify_playbook_by_query matches the correct subset of tools."""
    from routes.ai import classify_playbook_by_query
    
    # Test query on readiness
    tools_readiness = classify_playbook_by_query("Is live trading ready?")
    assert "get_live_readiness" in tools_readiness
    assert "get_feed_status" in tools_readiness
    # Should contain general tools too
    assert "get_risk_snapshot" in tools_readiness
    
    # Test query on why no trade
    tools_why = classify_playbook_by_query("why didn't we trade today?")
    assert "get_skipped_signals" in tools_why
    assert "get_logs_errors" in tools_why
    
    # Test query on scorecard/drawdown
    tools_scorecard = classify_playbook_by_query("Show me strategy loss streaks and score explained for strat-1")
    assert "get_strategy_scorecard" in tools_scorecard
    assert "get_strategy_score_explained" in tools_scorecard
    
    # Test default fallback for query with no matches
    tools_default = classify_playbook_by_query("Hello there")
    assert len(tools_default) < 10 # Should be small targeted subset, not all 24 tools
    assert "get_risk_snapshot" in tools_default


@pytest.mark.anyio
async def test_generate_gemini_embedding():
    """Verify generate_gemini_embedding requests Gemini embeddings and returns float list."""
    from core.embeddings import generate_gemini_embedding
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={"embedding": {"values": [0.1] * 768}})
    
    with patch("requests.post", return_value=mock_response), \
         patch.dict("os.environ", {"GEMINI_API_KEY": "fake_key"}):
        res = await generate_gemini_embedding("test text")
        assert len(res) == 768
        assert res[0] == 0.1


@pytest.mark.anyio
async def test_run_agent_tool_get_historical_context():
    """Verify get_historical_context collation of daily reports, events, and alerts."""
    mock_db = MagicMock()
    mock_db.agent_tool_audit.insert_one = AsyncMock()
    
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mock_cursor.to_list = AsyncMock(return_value=[{"date": "2026-06-22", "total_realized_pnl": 1000.0}])
    mock_db.daily_reports.find.return_value = mock_cursor
    
    mock_cursor_events = MagicMock()
    mock_cursor_events.sort.return_value = mock_cursor_events
    mock_cursor_events.to_list = AsyncMock(return_value=[{"created_at": "2026-06-22T10:00:00Z", "event": "test"}])
    mock_db.core_events.find.return_value = mock_cursor_events
    
    mock_cursor_alerts = MagicMock()
    mock_cursor_alerts.sort.return_value = mock_cursor_alerts
    mock_cursor_alerts.to_list = AsyncMock(return_value=[{"created_at": "2026-06-22T10:05:00Z", "message": "alert"}])
    mock_db.notifications.find.return_value = mock_cursor_alerts
    
    user = {"id": "test-trader-1"}
    with patch("routes.ai.db", mock_db):
        res = await _run_agent_tool("get_historical_context", user, query="Show history for 5 days")
        
    assert res["status"] == "ok"
    assert res["data"]["days_requested"] == 5
    assert len(res["data"]["daily_reports"]) == 1
    assert res["data"]["daily_reports"][0]["total_realized_pnl"] == 1000.0
    assert len(res["data"]["core_events"]) == 1
    assert len(res["data"]["alert_history"]) == 1


@pytest.mark.anyio
async def test_run_agent_tool_recall_memory():
    """Verify recall_memory cosine similarity lookup and time-decay calculations."""
    mock_db = MagicMock()
    mock_db.agent_tool_audit.insert_one = AsyncMock()
    
    mock_cursor = MagicMock()
    mock_cursor.sort.return_value = mock_cursor
    mem_vector = [0.1] * 768
    mock_cursor.to_list = AsyncMock(return_value=[{
        "text": "Nifty EMA Scalper made Rs 5000",
        "type": "daily_summary",
        "date": "2026-06-22",
        "embedding": mem_vector,
        "created_at": "2026-06-22T12:00:00Z"
    }])
    mock_db.hermes_memory.find.return_value = mock_cursor
    
    user = {"id": "test-trader-1"}
    
    with patch("routes.ai.db", mock_db), \
         patch("core.embeddings.generate_gemini_embedding", AsyncMock(return_value=[0.1] * 768)):
        res = await _run_agent_tool("recall_memory", user, query="Nifty performance")
        
    assert res["status"] == "ok"
    assert len(res["data"]) == 1
    assert res["data"][0]["similarity"] == 1.0
    assert res["data"][0]["decay_score"] <= 1.0
    assert res["data"][0]["text"] == "Nifty EMA Scalper made Rs 5000"


@pytest.mark.anyio
async def test_distill_daily_report_to_facts():
    """Verify EOD daily report fact distillation parses Gemini JSON results properly."""
    from core.embeddings import distill_daily_report_to_facts
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={"candidates": [{"content": {"parts": [{"text": '["Fact A", "Fact B"]'}]}}]})
    
    with patch("requests.post", return_value=mock_response), \
         patch.dict("os.environ", {"GEMINI_API_KEY": "fake_key"}):
        res = await distill_daily_report_to_facts("test report summary")
        assert len(res) == 2
        assert res[0] == "Fact A"


@pytest.mark.anyio
async def test_compile_eod_memory():
    """Verify that EOD compilation triggers fact distillation, embeds vectors, and drafts wiki note."""
    from position_monitor import _compile_eod_memory
    
    mock_db = MagicMock()
    
    mock_cursor_alerts = MagicMock()
    mock_cursor_alerts.to_list = AsyncMock(return_value=[])
    mock_db.notifications.find.return_value = mock_cursor_alerts
    
    mock_cursor_signals = MagicMock()
    mock_cursor_signals.to_list = AsyncMock(return_value=[])
    mock_db.signals.find.return_value = mock_cursor_signals
    
    mock_db.hermes_memory.insert_one = AsyncMock()
    mock_db.pending_actions.insert_one = AsyncMock()
    
    report_doc = {
        "total_realized_pnl": 2500.0,
        "total_unrealized_pnl": 0.0,
        "trades_taken": 3,
        "strategies": [{"name": "Strat 1", "realized_pnl": 2500.0, "trade_count": 3}]
    }
    
    mock_facts = ["Fact 1", "Fact 2"]
    mock_embedding = [0.05] * 768
    
    with patch("core.embeddings.distill_daily_report_to_facts", AsyncMock(return_value=mock_facts)), \
         patch("core.embeddings.generate_gemini_embedding", AsyncMock(return_value=mock_embedding)):
        await _compile_eod_memory(mock_db, "test-user", "2026-06-22", report_doc)
        
    assert mock_db.hermes_memory.insert_one.call_count == 2
    mock_db.pending_actions.insert_one.assert_called_once()
    action_doc = mock_db.pending_actions.insert_one.call_args[0][0]
    assert action_doc["action_type"] == "draft_wiki_note"
    assert action_doc["params"]["title"] == "Session Memory 2026-06-22"
    assert "## Daily Overview" in action_doc["params"]["body_markdown"]




