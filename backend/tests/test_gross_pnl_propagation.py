"""Regression test for Fix 3: exit P&L must carry a correct gross_pnl.

Bug: the paper-fill path set net_pnl on the exit order but left gross_pnl=0, so
the dashboard's Gross P&L always showed 0. The ledger computes the true gross
((exit-entry)*qty) but only returned net (realized_pnl); execution_router then
wrote net but never gross. The fix has the ledger return gross_pnl and the router
persist it, keeping the dashboard consistent (gross = net + charges).
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.portfolio_ledger import PortfolioLedger


class _Coll:
    def __init__(self, find_one_result=None):
        self._find_one_result = find_one_result
        self.inserted = []

    async def insert_one(self, doc, *a, **k):
        self.inserted.append(doc)
        return MagicMock(inserted_id="x")

    async def find_one(self, *a, **k):
        return self._find_one_result

    async def update_one(self, *a, **k):
        return MagicMock(modified_count=1)

    async def delete_one(self, *a, **k):
        return MagicMock(deleted_count=1)


class _FakeDB:
    def __init__(self, position):
        self._colls = {"strategy_positions": _Coll(find_one_result=position)}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._colls.setdefault(name, _Coll())


def _position(side, entry, qty, entry_charges):
    return {
        "id": "pos1",
        "user_id": "u1",
        "strategy_id": "s1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY 23000 CE 26 JUN 26",
        "underlying": "NIFTY",
        "mode": "paper",
        "status": "OPEN",
        "position_side": side,
        "open_quantity": qty,
        "average_buy_price": entry,
        "entry_charges": entry_charges,
        "created_at": "2026-06-12T03:00:00+00:00",
    }


def _exit_fill(side, exit_price, qty, exit_charges):
    return {
        "id": "fill_exit_1",
        "user_id": "u1",
        "strategy_id": "s1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY 23000 CE 26 JUN 26",
        "side": side,
        "qty": qty,
        "price": exit_price,
        "mode": "paper",
        "charges": exit_charges,
    }


def _close(position, fill):
    ledger = PortfolioLedger(_FakeDB(position))
    return asyncio.run(ledger.process_fill(fill))


def test_long_close_returns_true_gross_pnl():
    # entry 100 -> exit 120, 65 qty. gross = (120-100)*65 = 1300.
    res = _close(
        _position("LONG", entry=100.0, qty=65, entry_charges=8.0),
        _exit_fill("SELL", exit_price=120.0, qty=65, exit_charges=10.0),
    )
    assert res["accepted"] is True
    assert res["gross_pnl"] == 1300.0
    assert res["realized_pnl"] == 1300.0 - 10.0 - 8.0      # 1282.0
    # Dashboard consistency: gross == net + all charges.
    assert res["gross_pnl"] == res["realized_pnl"] + 10.0 + 8.0


def test_short_close_gross_pnl_sign():
    # SHORT entry 100 -> exit 80, 30 qty. gross = (100-80)*30 = 600.
    res = _close(
        _position("SHORT", entry=100.0, qty=30, entry_charges=5.0),
        _exit_fill("BUY", exit_price=80.0, qty=30, exit_charges=6.0),
    )
    assert res["accepted"] is True
    assert res["gross_pnl"] == 600.0
    assert res["realized_pnl"] == 600.0 - 6.0 - 5.0         # 589.0


def test_losing_close_gross_pnl_is_negative_but_above_net():
    # entry 100 -> exit 90, 65 qty. gross = -650; net is more negative (charges).
    res = _close(
        _position("LONG", entry=100.0, qty=65, entry_charges=8.0),
        _exit_fill("SELL", exit_price=90.0, qty=65, exit_charges=10.0),
    )
    assert res["gross_pnl"] == -650.0
    assert res["realized_pnl"] == -650.0 - 10.0 - 8.0       # -668.0
    assert res["gross_pnl"] > res["realized_pnl"]           # gross loss < net loss
