"""Tests for Phase 2 #5 — credit-spread lifecycle (core/spread_lifecycle.py).

Core invariant verified: the change in wallet balance over a full open→close
round trip equals the realized net P&L booked on the position. That proves the
two-leg money flow is conservative without hand-computing every charge.
"""
import asyncio
import sys
from pathlib import Path

import pytest
from pymongo.errors import DuplicateKeyError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.spread_lifecycle import (
    open_credit_spread,
    close_credit_spread,
    value_credit_spread,
    spread_exit_reason,
    compute_exit_levels,
)


# ── minimal in-memory async Mongo fake ──────────────────────────────────────────

def _match(doc, query):
    for k, v in query.items():
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Coll:
    def __init__(self, unique_field=None):
        self.docs = []
        self.unique_field = unique_field

    async def insert_one(self, doc):
        if self.unique_field is not None:
            uf = doc.get(self.unique_field)
            if any(d.get(self.unique_field) == uf for d in self.docs):
                raise DuplicateKeyError(f"dup {self.unique_field}={uf}")
        self.docs.append(dict(doc))
        return type("R", (), {"inserted_id": doc.get("id")})()

    async def find_one(self, query, proj=None):
        for d in self.docs:
            if _match(d, query):
                return dict(d)
        return None

    async def update_one(self, query, update, upsert=False):
        modified = 0
        for d in self.docs:
            if _match(d, query):
                for k, v in update.get("$set", {}).items():
                    d[k] = v
                for k, v in update.get("$inc", {}).items():
                    d[k] = (d.get(k) or 0) + v
                modified = 1
                break
        else:
            if upsert:
                nd = {k: v for k, v in query.items() if not isinstance(v, dict)}
                nd.update(update.get("$set", {}))
                for k, v in update.get("$inc", {}).items():
                    nd[k] = v
                self.docs.append(nd)
        return type("R", (), {"modified_count": modified})()

    async def replace_one(self, query, doc, upsert=False):
        for i, d in enumerate(self.docs):
            if _match(d, query):
                self.docs[i] = dict(doc)
                return type("R", (), {"modified_count": 1})()
        if upsert:
            self.docs.append(dict(doc))
        return type("R", (), {"modified_count": 0})()


class _DB:
    def __init__(self):
        self.strategy_positions = _Coll()
        self.orders = _Coll()
        self.trades = _Coll()
        self.strategies = _Coll()
        self.positions = _Coll()
        self.paper_wallets = _Coll(unique_field="user_id")
        self.paper_wallet_credits = _Coll(unique_field="order_id")


def _spread(short_premium=55.0, long_premium=30.0, width=100.0):
    return {
        "ok": True,
        "structure": "credit_spread",
        "direction": "bullish",
        "option_type": "PE",
        "short_leg": {"role": "short", "side": "SELL", "option_type": "PE", "strike": 22900,
                      "instrument_key": "PE|22900", "premium": short_premium, "expiry": "2026-06-30"},
        "long_leg": {"role": "long", "side": "BUY", "option_type": "PE", "strike": 22800,
                     "instrument_key": "PE|22800", "premium": long_premium, "expiry": "2026-06-30"},
        "net_credit": round(short_premium - long_premium, 2),
        "max_loss": round(width - (short_premium - long_premium), 2),
        "width_points": width,
        "net_delta": 0.10,
        "net_theta": 6.0,
    }


async def _balance(db, user_id="u1"):
    w = await db.paper_wallets.find_one({"user_id": user_id})
    return float(w["balance"])


def test_exit_levels():
    lv = compute_exit_levels(net_credit=25.0, width=100.0)
    assert lv["spread_tp_value"] == 12.5      # capture 50%
    assert lv["spread_sl_value"] == 75.0      # min(25*3, 100)


def test_exit_levels_capped_by_width():
    lv = compute_exit_levels(net_credit=40.0, width=100.0)
    assert lv["spread_sl_value"] == 100.0     # 40*3=120 capped to width 100


def test_value_and_exit_reason():
    pos = {"open_quantity": 50, "net_credit": 25.0, "spread_tp_value": 12.5, "spread_sl_value": 75.0}
    v = value_credit_spread(pos, short_ltp=20.0, long_ltp=8.0)  # value 12
    assert v["value"] == 12.0 and v["pnl"] == (25.0 - 12.0) * 50
    assert spread_exit_reason(pos, 12.0) == "spread-tp"
    assert spread_exit_reason(pos, 80.0) == "spread-sl"
    assert spread_exit_reason(pos, 20.0) is None


def test_open_credits_net_and_creates_position():
    db = _DB()

    async def run():
        await db.paper_wallets.insert_one({"user_id": "u1", "balance": 500000.0,
                                           "initial_balance": 500000.0, "total_debited": 0, "total_credited": 0})
        before = await _balance(db)
        res = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                       spread=_spread(), lots=1, lot_size=50, mode="paper",
                                       idempotency_key="spread:s1:0915")
        after = await _balance(db)
        pos = await db.strategy_positions.find_one({"id": res["id"]})
        return res, before, after, pos

    res, before, after, pos = asyncio.run(run())
    assert res["ok"] and res["status"] == "FILLED"
    # Net credit collected (≈ +1250) minus entry charges → balance rises by < 1250.
    assert before < after < before + 1250
    assert pos["structure"] == "credit_spread" and pos["position_side"] == "SHORT"
    assert len(pos["legs"]) == 2 and pos["open_quantity"] == 50
    assert pos["spread_tp_value"] == 12.5 and pos["spread_sl_value"] == 75.0


def test_open_then_close_wallet_equals_realized_pnl_win():
    db = _DB()

    async def run():
        await db.paper_wallets.insert_one({"user_id": "u1", "balance": 500000.0,
                                           "initial_balance": 500000.0, "total_debited": 0, "total_credited": 0})
        start = await _balance(db)
        res = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                       spread=_spread(), lots=1, lot_size=50, mode="paper",
                                       idempotency_key="spread:s1:0915")
        pos = await db.strategy_positions.find_one({"id": res["id"]})
        # Close at value 12 (TP): profit since 12 < net_credit 25.
        closed = await close_credit_spread(db, pos, reason="spread-tp", short_ltp=20.0, long_ltp=8.0)
        end = await _balance(db)
        final_pos = await db.strategy_positions.find_one({"id": res["id"]})
        return start, end, closed, final_pos

    start, end, closed, final_pos = asyncio.run(run())
    assert closed["ok"] and final_pos["status"] == "CLOSED"
    assert closed["realized_pnl"] > 0                       # winning spread
    # Invariant: wallet change over the round trip == realized net P&L.
    assert abs((end - start) - closed["realized_pnl"]) < 0.01
    assert abs(final_pos["realized_pnl"] - closed["realized_pnl"]) < 0.01


def test_open_then_close_wallet_equals_realized_pnl_loss():
    db = _DB()

    async def run():
        await db.paper_wallets.insert_one({"user_id": "u1", "balance": 500000.0,
                                           "initial_balance": 500000.0, "total_debited": 0, "total_credited": 0})
        start = await _balance(db)
        res = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                       spread=_spread(), lots=1, lot_size=50, mode="paper",
                                       idempotency_key="spread:s1:0915")
        pos = await db.strategy_positions.find_one({"id": res["id"]})
        # Close at value 75 (SL): loss since 75 > net_credit 25.
        closed = await close_credit_spread(db, pos, reason="spread-sl", short_ltp=95.0, long_ltp=20.0)
        end = await _balance(db)
        return start, end, closed

    start, end, closed = asyncio.run(run())
    assert closed["realized_pnl"] < 0                       # losing spread
    assert abs((end - start) - closed["realized_pnl"]) < 0.01


def test_open_idempotent():
    db = _DB()

    async def run():
        await db.paper_wallets.insert_one({"user_id": "u1", "balance": 500000.0,
                                           "initial_balance": 500000.0, "total_debited": 0, "total_credited": 0})
        r1 = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                      spread=_spread(), lots=1, lot_size=50, mode="paper",
                                      idempotency_key="spread:s1:0915")
        r2 = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                      spread=_spread(), lots=1, lot_size=50, mode="paper",
                                      idempotency_key="spread:s1:0915")
        n = len(db.strategy_positions.docs)
        return r1, r2, n

    r1, r2, n = asyncio.run(run())
    assert r1["ok"] and not r2["ok"] and r2["status"] == "SKIPPED" and n == 1


def test_double_close_skips():
    db = _DB()

    async def run():
        await db.paper_wallets.insert_one({"user_id": "u1", "balance": 500000.0,
                                           "initial_balance": 500000.0, "total_debited": 0, "total_credited": 0})
        res = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                       spread=_spread(), lots=1, lot_size=50, mode="paper",
                                       idempotency_key="spread:s1:0915")
        pos = await db.strategy_positions.find_one({"id": res["id"]})
        c1 = await close_credit_spread(db, pos, reason="spread-tp", short_ltp=20.0, long_ltp=8.0)
        c2 = await close_credit_spread(db, pos, reason="spread-tp", short_ltp=20.0, long_ltp=8.0)
        return c1, c2

    c1, c2 = asyncio.run(run())
    assert c1["ok"] and not c2["ok"] and c2["status"] == "SKIPPED"


def test_paper_only_guard():
    db = _DB()
    res = asyncio.run(open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                         spread=_spread(), lots=1, lot_size=50, mode="live",
                                         idempotency_key="k"))
    assert not res["ok"] and res["status"] == "SKIPPED"
