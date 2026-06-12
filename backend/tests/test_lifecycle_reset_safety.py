from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from order_lifecycle import (
    ORDER_FILLED,
    ORDER_NEW,
    ORDER_PLACED,
    ORDER_REJECTED,
    validate_order_transition,
)


def test_order_transition_allows_normal_submit_and_reject():
    assert validate_order_transition(ORDER_NEW, ORDER_PLACED) == ORDER_PLACED
    assert validate_order_transition(ORDER_NEW, ORDER_REJECTED) == ORDER_REJECTED


def test_order_transition_blocks_terminal_rewrite():
    with pytest.raises(ValueError):
        validate_order_transition(ORDER_FILLED, ORDER_REJECTED)


class _FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length):
        return self.rows


class _FakeCollection:
    def __init__(self, rows=None, count=0):
        self.rows = rows or []
        self.count = count
        self.inserted = []
        self.delete_many = AsyncMock(return_value=SimpleNamespace(deleted_count=0))
        self.delete_one = AsyncMock(return_value=SimpleNamespace(deleted_count=0))
        self.update_many = AsyncMock(return_value=SimpleNamespace(modified_count=0))
        self.update_one = AsyncMock(return_value=SimpleNamespace(modified_count=1))
        self.find_one_and_delete = AsyncMock(return_value=None)

    def find(self, *args, **kwargs):
        return _FakeCursor(self.rows)

    async def find_one(self, *args, **kwargs):
        return self.rows[0] if self.rows else None

    async def count_documents(self, *args, **kwargs):
        return self.count

    async def insert_one(self, doc):
        self.inserted.append(doc)
        return SimpleNamespace(inserted_id=doc.get("_id") or doc.get("id"))


class _FakeDb:
    def __init__(self):
        self.users = _FakeCollection(rows=[{"id": "u1", "email": "a@example.com"}], count=1)
        self.strategies = _FakeCollection(rows=[{"id": "s1"}], count=1)
        self.option_strategy_states = _FakeCollection()
        self._collections = {}

    def __getitem__(self, name):
        if name == "users":
            return self.users
        if name == "strategies":
            return self.strategies
        if name == "option_strategy_states":
            return self.option_strategy_states
        self._collections.setdefault(name, _FakeCollection(count=2))
        return self._collections[name]


@pytest.mark.asyncio
async def test_reset_all_accounts_requires_owner_and_exact_phrase():
    from routes import ops

    fake_db = _FakeDb()
    req = ops.OpsActionReq(confirm=False, note="")

    with patch.object(ops, "db", fake_db):
        res = await ops.ops_reset_all_accounts_trading_state(
            req=req,
            user={"id": "owner", "role": "owner"},
        )

    assert res["dry_run"] is True
    assert res["required_note"] == "RESET_ALL_ACCOUNTS_TO_PAPER"
    assert fake_db.users.update_many.await_count == 0
    assert fake_db.strategies.update_many.await_count == 0


@pytest.mark.asyncio
async def test_reset_all_accounts_executes_when_confirmed():
    from routes import ops

    fake_db = _FakeDb()
    req = ops.OpsActionReq(confirm=True, note="RESET_ALL_ACCOUNTS_TO_PAPER")

    async def fake_seed(user_id):
        return 1

    with patch.object(ops, "db", fake_db), patch.dict("sys.modules", {"server": MagicMock(seed_default_strategies_for_user=fake_seed)}):
        res = await ops.ops_reset_all_accounts_trading_state(
            req=req,
            user={"id": "owner", "role": "owner"},
        )

    assert res["ok"] is True
    assert fake_db.users.update_many.await_count == 1
    assert fake_db.strategies.update_many.await_count == 1


@pytest.mark.asyncio
async def test_reconciliation_breaks_reports_user_scoped_blockers():
    from routes import ops

    fake_db = _FakeDb()
    fake_db._collections["risk_state"] = _FakeCollection(rows=[{
        "_id": "position_reconciliation:u1",
        "user_id": "u1",
        "mismatch_detected": True,
        "mismatches": ["Quantity mismatch"],
    }])
    fake_db._collections["orders"] = _FakeCollection(rows=[{
        "id": "o-review",
        "user_id": "u1",
        "mode": "live",
        "status": "UNKNOWN_NEEDS_REVIEW",
    }])
    fake_db._collections["risk_reservations"] = _FakeCollection(rows=[])
    fake_db._collections["strategy_positions"] = _FakeCollection(rows=[])

    with patch.object(ops, "db", fake_db):
        res = await ops.ops_reconciliation_breaks(user={"id": "u1", "role": "owner"})

    assert res["blockers"]["position_reconciliation_mismatch"] is True
    assert res["blockers"]["unknown_live_orders"] == 1
    assert res["can_resolve_position_reconciliation"] is False


@pytest.mark.asyncio
async def test_reconciliation_resolve_is_dry_run_without_exact_phrase():
    from routes import ops

    fake_db = _FakeDb()
    fake_db._collections["risk_state"] = _FakeCollection(rows=[{
        "_id": "position_reconciliation:u1",
        "user_id": "u1",
        "mismatch_detected": True,
    }])
    fake_db._collections["orders"] = _FakeCollection(rows=[])
    fake_db._collections["risk_reservations"] = _FakeCollection(rows=[])
    fake_db._collections["strategy_positions"] = _FakeCollection(rows=[])
    req = ops.OpsActionReq(confirm=False, note="")

    with patch.object(ops, "db", fake_db):
        res = await ops.ops_resolve_reconciliation(req=req, user={"id": "u1", "role": "owner"})

    assert res["dry_run"] is True
    assert res["required_note"] == ops.RECONCILIATION_RESOLVE_PHRASE
    assert fake_db["risk_state"].update_one.await_count == 0


@pytest.mark.asyncio
async def test_reconciliation_resolve_blocks_when_unknown_orders_exist():
    from fastapi import HTTPException
    from routes import ops

    fake_db = _FakeDb()
    fake_db._collections["risk_state"] = _FakeCollection(rows=[{
        "_id": "position_reconciliation:u1",
        "user_id": "u1",
        "mismatch_detected": True,
    }])
    fake_db._collections["orders"] = _FakeCollection(rows=[{
        "id": "o-review",
        "user_id": "u1",
        "mode": "live",
        "status": "UNKNOWN_NEEDS_REVIEW",
    }])
    fake_db._collections["risk_reservations"] = _FakeCollection(rows=[])
    fake_db._collections["strategy_positions"] = _FakeCollection(rows=[])
    req = ops.OpsActionReq(confirm=True, note=ops.RECONCILIATION_RESOLVE_PHRASE)

    with patch.object(ops, "db", fake_db):
        with pytest.raises(HTTPException) as exc:
            await ops.ops_resolve_reconciliation(req=req, user={"id": "u1", "role": "owner"})

    assert exc.value.status_code == 409
    assert fake_db["risk_state"].update_one.await_count == 0


@pytest.mark.asyncio
async def test_reconciliation_resolve_clears_position_mismatch_with_audit():
    from routes import ops

    fake_db = _FakeDb()
    fake_db._collections["risk_state"] = _FakeCollection(rows=[{
        "_id": "position_reconciliation:u1",
        "user_id": "u1",
        "mismatch_detected": True,
        "mismatches": ["Quantity mismatch"],
    }])
    fake_db._collections["orders"] = _FakeCollection(rows=[])
    fake_db._collections["risk_reservations"] = _FakeCollection(rows=[])
    fake_db._collections["strategy_positions"] = _FakeCollection(rows=[])
    fake_db._collections["risk_events"] = _FakeCollection(rows=[])
    fake_db._collections["outbox_events"] = _FakeCollection(rows=[])
    req = ops.OpsActionReq(confirm=True, note=ops.RECONCILIATION_RESOLVE_PHRASE)

    with patch.object(ops, "db", fake_db):
        res = await ops.ops_resolve_reconciliation(req=req, user={"id": "u1", "role": "owner"})

    assert res["ok"] is True
    risk_update = fake_db["risk_state"].update_one.await_args.args[1]["$set"]
    assert risk_update["mismatch_detected"] is False
    assert fake_db["risk_events"].inserted[0]["event_type"] == "RECONCILIATION_MANUALLY_RESOLVED"
    assert fake_db["outbox_events"].inserted[0]["event_type"] == "RECONCILIATION_MANUALLY_RESOLVED"


@pytest.mark.asyncio
async def test_reserve_order_exposure_records_active_reservation():
    import server

    fake_db = MagicMock()
    fake_db.risk_reservation_locks = _FakeCollection()
    fake_db.risk_reservations = _FakeCollection(rows=[])
    fake_db.order_events = _FakeCollection()
    fake_db.outbox_events = _FakeCollection()

    with patch.object(server, "db", fake_db):
        doc = await server._reserve_order_exposure(
            user_id="u1",
            order_id="o1",
            strategy_id="s1",
            instrument_key="NSE_FO|1",
            symbol="NIFTYCE",
            quantity=10,
            price=50.0,
            settings={"max_position_size": 1000},
        )

    assert doc["status"] == "ACTIVE"
    assert doc["reserved_value"] == 500.0
    assert fake_db.risk_reservations.inserted[0]["order_id"] == "o1"


@pytest.mark.asyncio
async def test_reserve_order_exposure_blocks_over_reserved_limit():
    import server
    from fastapi import HTTPException

    fake_db = MagicMock()
    fake_db.risk_reservation_locks = _FakeCollection()
    fake_db.risk_reservations = _FakeCollection(rows=[{"reserved_value": 900.0}])
    fake_db.risk_events = _FakeCollection()
    fake_db.order_events = _FakeCollection()
    fake_db.outbox_events = _FakeCollection()

    with patch.object(server, "db", fake_db):
        with pytest.raises(HTTPException) as exc:
            await server._reserve_order_exposure(
                user_id="u1",
                order_id="o2",
                strategy_id="s1",
                instrument_key="NSE_FO|2",
                symbol="NIFTYPE",
                quantity=10,
                price=20.0,
                settings={"max_position_size": 1000},
            )

    assert exc.value.status_code == 409
    assert "reserved exposure" in exc.value.detail
    assert fake_db.risk_reservations.inserted == []


@pytest.mark.asyncio
async def test_execution_report_reducer_filled_settles_reservation():
    import server

    fake_db = MagicMock()
    fake_db.orders = _FakeCollection(rows=[{
        "id": "o1",
        "user_id": "u1",
        "broker_order_id": "b1",
        "status": "PLACED",
        "mode": "live",
        "broker": "upstox",
        "qty": 10,
        "strategy_id": "s1",
        "symbol": "NIFTYCE",
        "side": "BUY",
        "expected_price": 50.0,
        "order_intent": {"intent": "OPEN_LONG", "instrument": {"tradingsymbol": "NIFTYCE"}},
    }])
    fake_db.strategy_positions = _FakeCollection(rows=[{
        "id": "p1",
        "user_id": "u1",
        "entry_broker_order_id": "b1",
        "status": "PENDING_BROKER",
        "average_buy_price": 0,
        "tp_sl_tsl_config": {},
    }])
    fake_db.risk_reservations = _FakeCollection()
    fake_db.trade_fills = _FakeCollection()
    fake_db.trades = _FakeCollection()
    fake_db.order_events = _FakeCollection()
    fake_db.outbox_events = _FakeCollection()

    with patch.object(server, "db", fake_db):
        res = await server._advance_pending_order_from_broker(
            user_id="u1",
            broker_order_id="b1",
            status="COMPLETE",
            avg_price=55.5,
            filled_qty=10,
            pending_qty=0,
            status_message="complete",
            raw_report={"order_id": "b1"},
        )

    assert res["orders"] == 1
    fake_db.orders.update_one.assert_awaited()
    order_update = fake_db.orders.update_one.await_args_list[0].args[1]["$set"]
    assert order_update["status"] == "FILLED"
    assert order_update["filled_qty"] == 10
    assert fake_db.trade_fills.inserted[0]["mode"] == "live"
    assert fake_db.trade_fills.inserted[0]["order_id"] == "o1"
    assert fake_db.trade_fills.inserted[0]["fill_price"] == 55.5
    fake_db.risk_reservations.update_many.assert_awaited()
    reservation_update = fake_db.risk_reservations.update_many.await_args.args[1]["$set"]
    assert reservation_update["status"] == "SETTLED"


@pytest.mark.asyncio
async def test_execution_report_reducer_rejected_releases_reservation():
    import server

    fake_db = MagicMock()
    fake_db.orders = _FakeCollection(rows=[{
        "id": "o2",
        "user_id": "u1",
        "broker_order_id": "b2",
        "status": "PLACED",
        "qty": 10,
        "strategy_id": "s1",
        "order_intent": {"intent": "OPEN_LONG", "instrument": {"tradingsymbol": "NIFTYPE"}},
    }])
    fake_db.strategy_positions = _FakeCollection(rows=[])
    fake_db.strategy_position_locks = _FakeCollection()
    fake_db.risk_reservations = _FakeCollection()
    fake_db.order_events = _FakeCollection()
    fake_db.outbox_events = _FakeCollection()

    with patch.object(server, "db", fake_db):
        res = await server._advance_pending_order_from_broker(
            user_id="u1",
            broker_order_id="b2",
            status="REJECTED",
            status_message="margin rejected",
            raw_report={"order_id": "b2"},
        )

    assert res["orders"] == 1
    order_update = fake_db.orders.update_one.await_args.args[1]["$set"]
    assert order_update["status"] == "REJECTED"
    reservation_update = fake_db.risk_reservations.update_many.await_args.args[1]["$set"]
    assert reservation_update["status"] == "RELEASED"


@pytest.mark.asyncio
async def test_live_fill_booking_is_idempotent():
    import server

    existing_fill = {"id": "fill-existing", "order_id": "o1", "user_id": "u1", "mode": "live"}
    fake_db = MagicMock()
    fake_db.trade_fills = _FakeCollection(rows=[existing_fill])
    fake_db.orders = _FakeCollection()
    fake_db.order_events = _FakeCollection()
    fake_db.outbox_events = _FakeCollection()

    with patch.object(server, "db", fake_db):
        fill = await server._book_live_fill_from_order(
            {
                "id": "o1",
                "user_id": "u1",
                "mode": "live",
                "broker_order_id": "b1",
                "symbol": "NIFTYCE",
                "side": "BUY",
                "qty": 10,
                "price": 55.5,
                "order_intent": {"intent": "OPEN_LONG"},
            },
            fill_price=55.5,
            filled_qty=10,
        )

    assert fill == existing_fill
    assert fake_db.trade_fills.inserted == []
    assert fake_db.orders.update_one.await_count == 0


@pytest.mark.asyncio
async def test_fill_ledger_summary_uses_trade_fills_for_realized_pnl():
    import server

    fake_db = MagicMock()
    fake_db.trade_fills = _FakeCollection(rows=[
        {"user_id": "u1", "mode": "live", "strategy_id": "s1", "qty": 10, "fill_price": 100, "realized_pnl": 250, "brokerage": 20, "slippage": 5},
        {"user_id": "u1", "mode": "live", "strategy_id": "s1", "qty": 10, "fill_price": 80, "realized_pnl": -100, "brokerage": 20, "slippage": 3},
        {"user_id": "u1", "mode": "live", "strategy_id": "s1", "qty": 10, "fill_price": 90, "realized_pnl": 0, "brokerage": 20, "slippage": 0},
    ])

    with patch.object(server, "db", fake_db):
        summary = await server._fill_ledger_summary("u1", mode="live", strategy_id="s1")

    assert summary["source"] == "trade_fills"
    assert summary["fill_count"] == 3
    assert summary["closed_trade_count"] == 2
    assert summary["wins"] == 1
    assert summary["losses"] == 1
    assert summary["realized_pnl"] == 150
    assert summary["gross_turnover"] == 2700
    assert summary["brokerage"] == 60
