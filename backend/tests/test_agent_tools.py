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


