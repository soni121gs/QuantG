"""Tests for Phase 2 #3 — order-flow confirmation (order_flow.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import order_flow
from order_flow import orderflow_imbalance, orderflow_gate


def test_imbalance_positive():
    assert abs(orderflow_imbalance({"tbq": 700, "tsq": 300}) - 0.4) < 1e-9


def test_imbalance_balanced():
    assert orderflow_imbalance({"tbq": 500, "tsq": 500}) == 0.0


def test_imbalance_thin_book_returns_none(monkeypatch):
    monkeypatch.setattr(order_flow, "ORDERFLOW_MIN_VOLUME", 500.0)
    assert orderflow_imbalance({"tbq": 100, "tsq": 100}) is None  # total 200 < 500


def test_imbalance_missing_fields_returns_none():
    assert orderflow_imbalance({"tbq": 700}) is None
    assert orderflow_imbalance({}) is None


def test_buy_gate_passes_on_strong_buying(monkeypatch):
    monkeypatch.setattr(order_flow, "ORDERFLOW_MIN_IMBALANCE", 0.15)
    monkeypatch.setattr(order_flow, "ORDERFLOW_GATE_ENABLED", True)
    assert orderflow_gate(0.40, "BUY")["block"] is False


def test_buy_gate_blocks_on_weak_buying(monkeypatch):
    monkeypatch.setattr(order_flow, "ORDERFLOW_MIN_IMBALANCE", 0.15)
    monkeypatch.setattr(order_flow, "ORDERFLOW_GATE_ENABLED", True)
    monkeypatch.setattr(order_flow, "ORDERFLOW_GATE_SHADOW", False)
    d = orderflow_gate(0.05, "BUY")
    assert d["block"] is True and "ORDERFLOW_WEAK" in d["reason"]


def test_sell_gate_wants_selling_pressure(monkeypatch):
    monkeypatch.setattr(order_flow, "ORDERFLOW_MIN_IMBALANCE", 0.15)
    monkeypatch.setattr(order_flow, "ORDERFLOW_GATE_ENABLED", True)
    assert orderflow_gate(-0.40, "SELL")["block"] is False  # strong selling → ok
    assert orderflow_gate(0.05, "SELL")["block"] is True    # not selling → block


def test_shadow_does_not_block(monkeypatch):
    monkeypatch.setattr(order_flow, "ORDERFLOW_MIN_IMBALANCE", 0.15)
    monkeypatch.setattr(order_flow, "ORDERFLOW_GATE_ENABLED", False)
    monkeypatch.setattr(order_flow, "ORDERFLOW_GATE_SHADOW", True)
    d = orderflow_gate(0.05, "BUY")
    assert d["block"] is False and d["shadow"] is True


def test_off_is_noop(monkeypatch):
    monkeypatch.setattr(order_flow, "ORDERFLOW_MIN_IMBALANCE", 0.15)
    monkeypatch.setattr(order_flow, "ORDERFLOW_GATE_ENABLED", False)
    monkeypatch.setattr(order_flow, "ORDERFLOW_GATE_SHADOW", False)
    d = orderflow_gate(0.05, "BUY")
    assert d["block"] is False and d["shadow"] is False


def test_no_data_never_blocks():
    assert orderflow_gate(None, "BUY")["block"] is False
