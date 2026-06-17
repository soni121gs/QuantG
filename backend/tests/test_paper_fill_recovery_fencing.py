"""Stage 3 regression: legacy paper fill recovery must be fenced off when the
core engine (ExecutionRouter→PortfolioLedger) owns paper execution.

Under the core engine every paper order is booked synchronously through the
canonical ledger and inserted with paper_fill_applied=True (failures → REJECTED).
So _recover_pending_paper_fills has nothing legitimate to finish; running the
legacy applier would reintroduce the divergent second writer (db.positions
instead of strategy_positions). It must skip without touching orders or the
legacy applier. With the core engine off, it must still run the legacy path.
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_recovery_skipped_when_core_engine_owns_paper():
    import server

    mock_db = MagicMock()
    mock_db.orders.find = MagicMock()  # must NOT be queried when fenced off

    with patch.dict(os.environ, {"CORE_ENGINE_ENABLED": "true", "CORE_ENGINE_PAPER_ENABLED": "true"}), \
         patch("server.db", mock_db), \
         patch("server._apply_paper_fill_to_position", new_callable=AsyncMock) as mock_apply:
        result = asyncio.run(server._recover_pending_paper_fills())

    assert result == 0
    mock_apply.assert_not_called()
    mock_db.orders.find.assert_not_called()


def test_recovery_runs_legacy_path_when_core_engine_off():
    import server

    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[{"id": "o1", "user_id": "u1", "price": 100.0}])

    mock_db = MagicMock()
    mock_db.orders.find = MagicMock(return_value=cursor)

    with patch.dict(os.environ, {"CORE_ENGINE_ENABLED": "false", "CORE_ENGINE_PAPER_ENABLED": "false"}), \
         patch("server.db", mock_db), \
         patch("server._apply_paper_fill_to_position", new_callable=AsyncMock,
               return_value={"paper_fill_applied": True}) as mock_apply:
        result = asyncio.run(server._recover_pending_paper_fills())

    assert result == 1
    mock_apply.assert_called_once()
