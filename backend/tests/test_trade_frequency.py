import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import datetime, timezone, timedelta

from trade_frequency import check_frequency_gate, loss_streak_is_current


def test_loss_streak_is_current_resets_across_days():
    """The loss streak is an intraday breaker: only a same-IST-day last SL counts."""
    now = datetime.now(timezone.utc)
    assert loss_streak_is_current(now.isoformat()) is True
    # Yesterday's SL must NOT carry over (was the 0-trade-day bug).
    assert loss_streak_is_current((now - timedelta(days=1, hours=12)).isoformat()) is False
    # Missing/garbage timestamp is treated as not-current (no carry-over).
    assert loss_streak_is_current(None) is False
    assert loss_streak_is_current("not-a-date") is False


@pytest.mark.anyio
async def test_frequency_gate_ignores_stale_loss_streak():
    """A high streak from a previous day must not throttle/block today."""
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    class _Orders:
        async def count_documents(self, query):
            return 1

    class _Strategies:
        find_one = AsyncMock(return_value={"today_pnl": 0.0})

    class _LossStreaks:
        find_one = AsyncMock(return_value={"current_streak": 9, "paused_until": None, "last_sl_at": stale})

    class _DB:
        orders = _Orders()
        strategies = _Strategies()
        strategy_loss_streaks = _LossStreaks()

    ok, reason = await check_frequency_gate(
        _DB(), strategy_id="s1", strategy_name="NIFTY HFT Quick Scalper", user_id="u1",
    )
    assert ok, f"stale streak should not block, got reason={reason}"


@pytest.mark.anyio
async def test_frequency_gate_counts_buy_entries_only_and_boosts_profitable_strategy():
    captured = {}

    class _Orders:
        async def count_documents(self, query):
            captured["query"] = query
            return 4

    class _Strategies:
        find_one = AsyncMock(return_value={"today_pnl": 1200.0})

    class _LossStreaks:
        find_one = AsyncMock(return_value=None)

    class _DB:
        orders = _Orders()
        strategies = _Strategies()
        strategy_loss_streaks = _LossStreaks()

    ok, reason = await check_frequency_gate(
        _DB(),
        strategy_id="strat-1",
        strategy_name="NIFTY Momentum Buyer",
        user_id="user-1",
        freq_multiplier=0.5,
    )

    assert ok
    assert reason is None
    assert captured["query"]["side"] == "BUY"
    assert captured["query"]["$or"][1]["idempotency_key"]["$not"]["$regex"] == "^exit:"


@pytest.mark.anyio
async def test_frequency_gate_blocks_when_true_buy_entries_hit_boosted_cap():
    class _Orders:
        async def count_documents(self, query):
            return 10

    class _Strategies:
        find_one = AsyncMock(return_value={"today_pnl": 1200.0})

    class _LossStreaks:
        find_one = AsyncMock(return_value=None)

    class _DB:
        orders = _Orders()
        strategies = _Strategies()
        strategy_loss_streaks = _LossStreaks()

    ok, reason = await check_frequency_gate(
        _DB(),
        strategy_id="strat-1",
        strategy_name="NIFTY Momentum Buyer",
        user_id="user-1",
        freq_multiplier=0.5,
    )

    assert not ok
    assert "DAILY_CAP: 10/10" in reason
    assert "profit_boost=2" in reason
