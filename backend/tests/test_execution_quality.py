from core.execution_quality import build_quality_doc, quality_grade


def test_buy_fill_records_adverse_slippage_and_cost_bps():
    doc = build_quality_doc(
        order={
            "id": "ord1",
            "user_id": "u1",
            "strategy_id": "s1",
            "target_symbol": "NIFTY 25000 CE",
            "side": "BUY",
            "qty": 50,
            "requested_price": 100,
            "price": 101,
            "charges": 25,
            "status": "FILLED",
            "mode": "paper",
            "created_at": "2026-09-04T09:30:00+00:00",
            "updated_at": "2026-09-04T09:30:01+00:00",
        },
        event="paper_fill",
    )

    assert doc["adverse_slippage_per_unit"] == 1
    assert doc["slippage_amount"] == 50
    assert doc["cost_amount"] == 75
    assert doc["fill_delay_ms"] == 1000
    assert doc["quality_grade"] == "EXPENSIVE"


def test_sell_fill_records_adverse_slippage_when_fill_is_below_expected():
    doc = build_quality_doc(
        order={
            "id": "ord2",
            "user_id": "u1",
            "strategy_id": "s1",
            "side": "SELL",
            "qty": 25,
            "requested_price": 80,
            "price": 79.5,
            "charges": 5,
            "status": "FILLED",
        },
        event="paper_fill",
    )

    assert doc["signed_slippage_per_unit"] == 0.5
    assert doc["slippage_amount"] == 12.5
    assert doc["cost_amount"] == 17.5


def test_rejected_fill_is_missed_not_expensive():
    doc = build_quality_doc(
        order={
            "id": "ord3",
            "user_id": "u1",
            "strategy_id": "s1",
            "side": "BUY",
            "qty": 10,
            "requested_price": 100,
            "price": 0,
            "status": "REJECTED",
        },
        event="paper_fill",
        status="REJECTED",
        reason="STALE_QUOTE",
    )

    assert doc["quality_grade"] == "MISSED"
    assert doc["missed_fill_reason"] == "STALE_QUOTE"


def test_quality_grade_thresholds():
    assert quality_grade(1, 1, 10000, "FILLED") == "GOOD"
    assert quality_grade(10, 10, 10000, "FILLED") == "OK"
    assert quality_grade(20, 20, 10000, "FILLED") == "EXPENSIVE"
    assert quality_grade(0, 0, 0, "FILLED") == "UNKNOWN"
