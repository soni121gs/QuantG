from unittest.mock import AsyncMock, MagicMock

import pytest

from core.portfolio_ledger import PortfolioLedger


@pytest.mark.anyio
async def test_mark_update_is_status_guarded():
    db = MagicMock()
    db.strategy_positions.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    await PortfolioLedger(db).update_position_mark(
        position_id="p1", user_id="u1", fields={"last_ltp": 100},
    )
    query, update = db.strategy_positions.update_one.await_args.args
    assert query["id"] == "p1" and query["user_id"] == "u1"
    assert set(query["status"]["$in"]) == {"PENDING_BROKER", "OPEN", "FILLED"}
    assert update["$set"]["last_ltp"] == 100


@pytest.mark.anyio
async def test_mark_update_can_clear_last_error():
    db = MagicMock()
    db.strategy_positions.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    await PortfolioLedger(db).update_position_mark(
        position_id="p1", user_id="u1", fields={"unrealized_pnl": 5},
        clear_last_error=True,
    )
    update = db.strategy_positions.update_one.await_args.args[1]
    assert update["$unset"] == {"last_error": ""}
