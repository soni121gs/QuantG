"""Stage 2b regression: PerformanceTracker time-bracket P&L must come from the
canonical ledger (db.trade_fills), not strategy_positions.

Before the fix, compile_strategy_scorecard summed strategy_positions.realized_pnl
for today/7d/30d. The legacy paper fill engine (_apply_paper_fill_to_position)
never writes strategy_positions, so legacy-filled trades were silently missing.
This test asserts the scorecard reads trade_fills, honours the CLOSE/REDUCE
action filter and the 30-day window, and buckets cumulatively (today ⊂ 7d ⊂ 30d).
"""
import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.performance_tracker import PerformanceTracker


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs[: length] if length else self._docs)


class _FillsColl:
    """Query-aware fake honouring action {$in} and created_at {$gte}, like Mongo."""

    def __init__(self, docs):
        self._docs = docs

    def find(self, query=None, projection=None):
        query = query or {}
        action_in = (query.get("action") or {}).get("$in")
        gte = (query.get("created_at") or {}).get("$gte")
        out = []
        for d in self._docs:
            if action_in is not None and d.get("action") not in action_in:
                continue
            if gte is not None and str(d.get("created_at") or "") < str(gte):
                continue
            out.append(d)
        return _Cursor(out)


class _EmptyColl:
    def find(self, *a, **k):
        return _Cursor([])

    async def find_one(self, *a, **k):
        return None

    async def update_one(self, *a, **k):
        return MagicMock(modified_count=1)


class _FakeDB:
    def __init__(self, fills):
        self._colls = {"trade_fills": _FillsColl(fills)}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, _EmptyColl())


def _iso(days_ago=0, hours_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)).isoformat()


def test_time_bracket_pnl_reads_trade_fills_with_action_and_window_filters():
    fills = [
        {"action": "CLOSE", "realized_pnl": 500.0, "created_at": _iso(hours_ago=1)},   # today
        {"action": "REDUCE", "realized_pnl": 200.0, "created_at": _iso(days_ago=5)},    # within 7d
        {"action": "CLOSE", "realized_pnl": -100.0, "created_at": _iso(days_ago=20)},   # within 30d
        {"action": "CLOSE", "realized_pnl": 9999.0, "created_at": _iso(days_ago=40)},   # outside 30d (filtered)
        {"action": "OPEN", "realized_pnl": 777.0, "created_at": _iso(hours_ago=2)},     # wrong action (filtered)
    ]
    tracker = PerformanceTracker(_FakeDB(fills))
    card = asyncio.run(tracker.compile_strategy_scorecard("s1", "u1"))

    assert card["today_pnl"] == 500.0
    assert card["seven_day_pnl"] == 700.0        # 500 + 200
    assert card["thirty_day_pnl"] == 600.0       # 500 + 200 - 100; 40d and OPEN excluded


def test_no_closing_fills_yields_zero_brackets():
    tracker = PerformanceTracker(_FakeDB([]))
    card = asyncio.run(tracker.compile_strategy_scorecard("s1", "u1"))
    assert card["today_pnl"] == 0.0
    assert card["seven_day_pnl"] == 0.0
    assert card["thirty_day_pnl"] == 0.0
