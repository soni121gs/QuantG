from unittest.mock import MagicMock, patch

import pytest

from core.embeddings import DEFAULT_CHAT_MODEL, DEFAULT_EMBEDDING_MODEL, generate_gemini_embedding
from routes.ai import DEFAULT_GEMINI_MODEL, _tool_planner_declarations


def test_p4_default_chat_model_is_gemini_3_flash_preview():
    assert DEFAULT_GEMINI_MODEL == "gemini-3-flash-preview"
    assert DEFAULT_CHAT_MODEL == "gemini-3-flash-preview"


def test_tool_planner_declarations_still_available():
    declarations = _tool_planner_declarations()
    names = {d["name"] for d in declarations}
    assert "get_live_readiness" in names
    assert "get_upstox_data_health" in names


@pytest.mark.anyio
async def test_embedding_model_and_dimension_parity(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    mock_response = MagicMock()
    mock_response.json.return_value = {"embedding": {"values": [0.1] * 768}}
    mock_response.raise_for_status.return_value = None

    with patch("core.embeddings.requests.post", return_value=mock_response) as post:
        vec = await generate_gemini_embedding("test text")

    assert DEFAULT_EMBEDDING_MODEL == "gemini-embedding-001"
    assert len(vec) == 768
    assert DEFAULT_EMBEDDING_MODEL in post.call_args.args[0]
