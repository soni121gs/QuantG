"""Regression tests for lot-sizing caps.

Bug: a strategy configured for 1 lot (options.lots=1, risk.max_lot=1) bought
2-9 lots because the adaptive capital allocator scaled the base quantity with no
ceiling, and compute_position_size never honoured max_lot. These tests pin both
layers: the pure sizer must respect max_lot, and RiskManager.evaluate_order must
clamp the allocator's output to the configured lot ceiling regardless of wallet
equity.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.risk_manager as rm_mod
from core.risk_manager import RiskManager
from risk_controls import SizeInputs, compute_position_size


# --- Layer 1: pure sizer honours max_lot ------------------------------------

def test_compute_position_size_caps_at_max_lot():
    # Big wallet + a large requested qty would otherwise allow many lots; max_lot
    # must clamp the result to exactly one lot.
    result = compute_position_size(SizeInputs(
        equity=500_000,
        free_margin=500_000,
        requested_qty=65 * 10,      # caller asked for 10 lots
        lot_size=65,
        entry_price=152.0,
        stop_loss_price=140.0,
        max_position_value=500_000,
        daily_loss_limit=0,
        risk_style="momentum",
        max_lot=1,
    ))
    assert result.allowed
    assert result.quantity == 65          # exactly one NIFTY lot
    assert result.caps["max_lot"] == 65


def test_compute_position_size_max_lot_allows_configured_multiple():
    result = compute_position_size(SizeInputs(
        equity=500_000,
        free_margin=500_000,
        requested_qty=30 * 20,
        lot_size=30,
        entry_price=200.0,
        stop_loss_price=180.0,
        max_position_value=500_000,
        daily_loss_limit=0,
        risk_style="breakout",
        max_lot=3,
    ))
    assert result.allowed
    assert result.quantity == 90          # exactly three BANKNIFTY lots


# --- Layer 2: evaluate_order clamps allocator inflation ----------------------

class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs)


class _FakeCollection:
    def __init__(self, one=None, many=None):
        self._one = one
        self._many = many or []

    async def find_one(self, *args, **kwargs):
        return self._one

    def find(self, *args, **kwargs):
        return _FakeCursor(self._many)


class _FakeDB:
    def __init__(self, **collections):
        self._c = collections

    def __getattr__(self, name):
        return self._c.get(name, _FakeCollection())


def _strategy(lots, max_lot, underlying="NIFTY"):
    return {
        "id": "strat-1",
        "user_id": "user-1",
        "today_pnl": 0.0,
        "visual_config": {
            "options": {"lots": lots},
            "risk": {"max_lot": max_lot, "risk_style": "momentum"},
        },
    }


def _run_evaluate(strategy, *, lot_size, price, requested_lots, alloc_mult, monkeypatch):
    db = _FakeDB(
        risk_state=_FakeCollection(one=None),
        strategies=_FakeCollection(one=strategy),
        users=_FakeCollection(one={"id": "user-1", "settings": {}}),
        paper_wallets=_FakeCollection(one={"balance": 500_000}),
        strategy_positions=_FakeCollection(many=[]),
    )
    # Market always open and a high allocator multiplier (the bug's trigger).
    monkeypatch.setattr(rm_mod.MarketSessionService, "is_segment_open", staticmethod(lambda *a, **k: True))

    async def _fake_mult(*args, **kwargs):
        return alloc_mult

    import core.capital_allocator as alloc_mod
    monkeypatch.setattr(alloc_mod, "get_strategy_allocation_multiplier", _fake_mult)

    return asyncio.run(RiskManager(db).evaluate_order(
        user_id="user-1",
        strategy_id="strat-1",
        symbol="NIFTY",
        target_symbol="NIFTY 23350 CE 16 JUN 26",  # verbose form -> Greeks proxy skipped
        side="BUY",
        requested_qty=requested_lots * lot_size,
        price=price,
        mode="paper",
        stop_loss=round(price * 0.92, 2),
        lot_size=lot_size,
        risk_style="momentum",
    ))


def test_evaluate_order_clamps_allocator_to_one_lot(monkeypatch):
    # Reproduces the live bug: 1-lot config, allocator wants 9x, 500k wallet.
    res = _run_evaluate(
        _strategy(lots=1, max_lot=1),
        lot_size=65, price=152.03, requested_lots=1, alloc_mult=9.0,
        monkeypatch=monkeypatch,
    )
    assert res["ok"] is True
    assert res["quantity"] == 65          # NOT 9 lots (585)


def test_evaluate_order_respects_configurable_max_lot(monkeypatch):
    # max_lot=3 -> allocator may scale up to but not beyond 3 lots.
    res = _run_evaluate(
        _strategy(lots=1, max_lot=3),
        lot_size=30, price=200.0, requested_lots=1, alloc_mult=9.0,
        monkeypatch=monkeypatch,
    )
    assert res["ok"] is True
    assert res["quantity"] == 90          # capped at 3 BANKNIFTY lots
