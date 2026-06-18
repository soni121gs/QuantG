import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import position_monitor
from core.portfolio_ledger import get_strategy_pnl_today
from migration.backfill_spread_trade_fills import build_spread_trade_fill


def _match_value(value, condition):
    if isinstance(condition, dict):
        if "$in" in condition and value not in condition["$in"]:
            return False
        if "$gte" in condition and str(value) < str(condition["$gte"]):
            return False
        if "$lt" in condition and str(value) >= str(condition["$lt"]):
            return False
        return True
    return value == condition


def _match(doc, query):
    for key, condition in (query or {}).items():
        if key == "$or":
            if not any(_match(doc, branch) for branch in condition):
                return False
        elif key == "$and":
            if not all(_match(doc, branch) for branch in condition):
                return False
        elif not _match_value(doc.get(key), condition):
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return list(self.docs[:length] if length else self.docs)


class _AsyncCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.updated = []

    def find(self, query=None, projection=None):
        return _Cursor([dict(doc) for doc in self.docs if _match(doc, query or {})])

    async def distinct(self, field):
        return sorted({doc.get(field) for doc in self.docs if doc.get(field) is not None})

    async def count_documents(self, query):
        return len([doc for doc in self.docs if _match(doc, query)])

    async def update_one(self, query, update, upsert=False):
        self.updated.append((query, update, upsert))
        return type("R", (), {"modified_count": 1})()


class _DB:
    def __init__(self, fills=None, positions=None, strategies=None, signals=None):
        self.trade_fills = _AsyncCollection(fills)
        self.strategy_positions = _AsyncCollection(positions)
        self.strategies = _AsyncCollection(strategies)
        self.signals = _AsyncCollection(signals)
        self.daily_reports = _AsyncCollection()


@pytest.mark.asyncio
async def test_get_strategy_pnl_today_can_rebuild_specific_ist_date():
    db = _DB(
        fills=[
            {
                "user_id": "u1",
                "strategy_id": "s1",
                "action": "CLOSE",
                "realized_pnl": -100.0,
                "created_at": "2026-06-18T09:00:00+00:00",
            },
            {
                "user_id": "u1",
                "strategy_id": "s1",
                "action": "CLOSE",
                "realized_pnl": 999.0,
                "created_at": "2026-06-19T09:00:00+00:00",
            },
        ]
    )

    pnl = await get_strategy_pnl_today(db, "s1", "u1", trading_date="2026-06-18")

    assert pnl["realized_pnl"] == -100.0
    assert pnl["trade_count"] == 1


@pytest.mark.asyncio
async def test_eod_aggregation_uses_report_date_for_strategy_pnl(monkeypatch):
    db = _DB(
        positions=[{"user_id": "u1"}],
        strategies=[{"user_id": "u1", "id": "s1", "name": "Strategy 1", "status": "paused"}],
    )
    calls = []

    async def fake_pnl(_db, strategy_id, user_id, trading_date=None):
        calls.append((strategy_id, user_id, trading_date))
        return {"realized_pnl": -42.0, "unrealized_pnl": 0.0, "trade_count": 1}

    monkeypatch.setattr(position_monitor, "get_strategy_pnl_today", fake_pnl)

    await position_monitor._run_eod_aggregation(db, report_date="2026-06-18")

    assert calls == [("s1", "u1", "2026-06-18")]
    query, update, upsert = db.daily_reports.updated[0]
    assert query == {"user_id": "u1", "date": "2026-06-18"}
    assert update["$set"]["total_realized_pnl"] == -42.0
    assert update["$set"]["trades_taken"] == 1
    assert upsert is True


def test_spread_backfill_builds_canonical_close_fill_doc():
    doc = build_spread_trade_fill(
        {
            "id": "pos_1",
            "user_id": "u1",
            "strategy_id": "s1",
            "symbol": "NIFTY",
            "target_symbol": "NIFTY 23950/23850 PE CREDIT",
            "structure": "credit_spread",
            "quantity": 260,
            "realized_pnl": -752.92,
            "gross_pnl": -730.0,
            "closed_at": "2026-06-18T08:39:21+00:00",
            "exit_reason": "strategy-sell-signal",
            "mode": "paper",
        }
    )

    assert doc["id"] == "tf_spread_pos_1"
    assert doc["order_id"] == "backfill:spread:pos_1"
    assert doc["action"] == "CLOSE"
    assert doc["asset_type"] == "option_spread"
    assert doc["realized_pnl"] == -752.92
    assert doc["charges"] == 22.92
    assert doc["created_at"] == "2026-06-18T08:39:21+00:00"
    assert doc["backfilled"] is True
