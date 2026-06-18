import pytest

from core.event_store import CoreEventStore


class _FakeCoreEvents:
    def __init__(self):
        self.inserted = None

    async def insert_one(self, doc):
        self.inserted = doc


class _FakeDb:
    def __init__(self):
        self.core_events = _FakeCoreEvents()


@pytest.mark.asyncio
async def test_log_signal_event_writes_stage1_envelope():
    db = _FakeDb()
    sig = {
        "id": "sig-123",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "status": "PENDING",
        "symbol": "nifty",
        "target_symbol": "NIFTY24JUN24000CE",
        "action": "buy",
        "mode": "paper",
    }

    event_doc = await CoreEventStore(db).log_signal_event(
        "signal_processed",
        sig,
        payload={
            "signal_status": "PROCESSED",
            "order_id": "ord-1",
            "detail": {"ok": True},
        },
    )

    assert db.core_events.inserted == event_doc
    assert event_doc["_id"].startswith("evt_")
    assert event_doc["event_id"] == event_doc["_id"]
    assert event_doc["event_type"] == "SIGNAL_PROCESSED"
    assert event_doc["schema_version"] == 1
    assert event_doc["user_id"] == "user-1"
    assert event_doc["strategy_id"] == "strat-1"
    assert event_doc["correlation_id"] == "corr:sig-123"
    assert event_doc["causation_id"] == "record:signals:sig-123"
    assert event_doc["idempotency_key"] == "signal:SIGNAL_PROCESSED:sig-123"
    assert event_doc["source_module"] == "signal_manager"
    assert event_doc["payload"] == {
        "signal_id": "sig-123",
        "signal_status": "PROCESSED",
        "order_id": "ord-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY24JUN24000CE",
        "action": "BUY",
        "mode": "paper",
        "detail": {"ok": True},
    }
