"""Tests for Phase 2 #5b — debit-spread builder and lifecycle (core/spread_builder.py, core/spread_lifecycle.py)."""
import asyncio
import sys
from pathlib import Path

import pytest
from pymongo.errors import DuplicateKeyError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.spread_builder import build_debit_spread
from core.spread_lifecycle import (
    open_debit_spread,
    close_debit_spread,
    value_debit_spread,
    debit_spread_exit_reason,
    compute_debit_exit_levels,
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
        self.trade_fills = _Coll(unique_field="id")
        self.strategies = _Coll()
        self.positions = _Coll()
        self.paper_wallets = _Coll(unique_field="user_id")
        self.paper_wallet_credits = _Coll(unique_field="order_id")


def _leg(key, delta, ltp, theta=-5.0):
    return {
        "instrument_key": key,
        "option_greeks": {"delta": delta, "iv": 15.0, "theta": theta},
        "market_data": {"ltp": ltp, "oi": 100000},
    }


def _node(strike, ce=None, pe=None, expiry="2026-06-30"):
    n = {"strike_price": strike, "expiry": expiry}
    if ce is not None:
        n["call_options"] = ce
    if pe is not None:
        n["put_options"] = pe
    return n


def _ce_chain():
    return [
        _node(23000, ce=_leg("CE|23000", 0.50, 100)),            # long (ATM, delta near 0.50)
        _node(23050, ce=_leg("CE|23050", 0.40, 75)),
        _node(23100, ce=_leg("CE|23100", 0.30, 55, theta=-10)),  # short (OTM, width 100 strikes)
        _node(23150, ce=_leg("CE|23150", 0.25, 40)),
        _node(23200, ce=_leg("CE|23200", 0.20, 30, theta=-4)),
    ]


def _pe_chain():
    return [
        _node(22800, pe=_leg("PE|22800", -0.20, 30, theta=-4)),
        _node(22850, pe=_leg("PE|22850", -0.25, 40)),
        _node(22900, pe=_leg("PE|22900", -0.30, 55, theta=-10)),  # short (OTM)
        _node(22950, pe=_leg("PE|22950", -0.40, 75)),
        _node(23000, pe=_leg("PE|23000", -0.50, 100)),            # long (ATM)
    ]


def _spread(long_premium=100.0, short_premium=55.0, width=100.0):
    return {
        "ok": True,
        "structure": "debit_spread",
        "direction": "bullish",
        "option_type": "CE",
        "short_leg": {"role": "short", "side": "SELL", "option_type": "CE", "strike": 23100,
                      "instrument_key": "CE|23100", "premium": short_premium, "expiry": "2026-06-30"},
        "long_leg": {"role": "long", "side": "BUY", "option_type": "CE", "strike": 23000,
                     "instrument_key": "CE|23000", "premium": long_premium, "expiry": "2026-06-30"},
        "net_debit": round(long_premium - short_premium, 2),
        "max_loss": round(long_premium - short_premium, 2),
        "max_profit": round(width - (long_premium - short_premium), 2),
        "width_points": width,
        "net_delta": 0.20,
        "net_theta": 5.0,
    }


async def _balance(db, user_id="u1"):
    w = await db.paper_wallets.find_one({"user_id": user_id})
    return float(w["balance"])


def test_build_debit_spread_bullish():
    s = build_debit_spread(chain_nodes=_ce_chain(), direction="bullish", width_points=100, long_delta=0.50)
    assert s["ok"] is True
    assert s["structure"] == "debit_spread"
    assert s["option_type"] == "CE"
    assert s["long_leg"]["strike"] == 23000 and s["long_leg"]["side"] == "BUY"
    assert s["short_leg"]["strike"] == 23100 and s["short_leg"]["side"] == "SELL"
    assert s["net_debit"] == 45.0       # 100 - 55
    assert s["max_loss"] == 45.0        # same as net_debit
    assert s["max_profit"] == 55.0      # width 100 - 45 debit
    assert s["width_points"] == 100


def test_build_debit_spread_bearish():
    s = build_debit_spread(chain_nodes=_pe_chain(), direction="bearish", width_points=100, long_delta=0.50)
    assert s["ok"] is True
    assert s["structure"] == "debit_spread"
    assert s["option_type"] == "PE"
    assert s["long_leg"]["strike"] == 23000 and s["long_leg"]["side"] == "BUY"
    assert s["short_leg"]["strike"] == 22900 and s["short_leg"]["side"] == "SELL"
    assert s["net_debit"] == 45.0
    assert s["max_profit"] == 55.0


def test_debit_exit_levels():
    lv = compute_debit_exit_levels(net_debit=45.0, width=100.0)
    assert lv["spread_tp_value"] == 45.0 + (100.0 - 45.0) * 0.5   # 45 + 27.5 = 72.5
    assert lv["spread_sl_value"] == 45.0 * 0.5                  # 22.5


def test_debit_value_and_exit_reason():
    pos = {"open_quantity": 50, "net_debit": 45.0, "spread_tp_value": 72.5, "spread_sl_value": 22.5}
    v = value_debit_spread(pos, short_ltp=40.0, long_ltp=90.0)  # value = 90 - 40 = 50
    assert v["value"] == 50.0 and v["pnl"] == (50.0 - 45.0) * 50
    assert debit_spread_exit_reason(pos, 73.0) == "spread-tp"
    assert debit_spread_exit_reason(pos, 20.0) == "spread-sl"
    assert debit_spread_exit_reason(pos, 50.0) is None


def test_open_debits_net_and_creates_position():
    db = _DB()

    async def run():
        await db.paper_wallets.insert_one({"user_id": "u1", "balance": 500000.0,
                                           "initial_balance": 500000.0, "total_debited": 0, "total_credited": 0})
        before = await _balance(db)
        res = await open_debit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                      spread=_spread(), lots=1, lot_size=50, mode="paper",
                                      idempotency_key="spread:s1:0915")
        after = await _balance(db)
        pos = await db.strategy_positions.find_one({"id": res["id"]})
        return res, before, after, pos

    res, before, after, pos = asyncio.run(run())
    assert res["ok"] and res["status"] == "FILLED"
    # Net debit paid (≈ -2250) + entry charges → balance falls by > 2250.
    assert after < before - 2250
    assert pos["structure"] == "debit_spread" and pos["position_side"] == "LONG"
    assert len(pos["legs"]) == 2 and pos["open_quantity"] == 50
    assert pos["spread_tp_value"] == 72.5 and pos["spread_sl_value"] == 22.5


def test_open_then_close_wallet_equals_realized_pnl_win():
    db = _DB()

    async def run():
        await db.paper_wallets.insert_one({"user_id": "u1", "balance": 500000.0,
                                           "initial_balance": 500000.0, "total_debited": 0, "total_credited": 0})
        start = await _balance(db)
        res = await open_debit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                      spread=_spread(), lots=1, lot_size=50, mode="paper",
                                      idempotency_key="spread:s1:0915")
        pos = await db.strategy_positions.find_one({"id": res["id"]})
        # Close at value 72.5 (TP): profit since 72.5 > net_debit 45.
        closed = await close_debit_spread(db, pos, reason="spread-tp", short_ltp=17.5, long_ltp=90.0)
        end = await _balance(db)
        final_pos = await db.strategy_positions.find_one({"id": res["id"]})
        return start, end, closed, final_pos

    start, end, closed, final_pos = asyncio.run(run())
    assert closed["ok"] and final_pos["status"] == "CLOSED"
    assert closed["realized_pnl"] > 0
    # Invariant: wallet change over the round trip == realized net P&L.
    assert abs((end - start) - closed["realized_pnl"]) < 0.01
    assert abs(final_pos["realized_pnl"] - closed["realized_pnl"]) < 0.01


def test_open_then_close_wallet_equals_realized_pnl_loss():
    db = _DB()

    async def run():
        await db.paper_wallets.insert_one({"user_id": "u1", "balance": 500000.0,
                                           "initial_balance": 500000.0, "total_debited": 0, "total_credited": 0})
        start = await _balance(db)
        res = await open_debit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                      spread=_spread(), lots=1, lot_size=50, mode="paper",
                                      idempotency_key="spread:s1:0915")
        pos = await db.strategy_positions.find_one({"id": res["id"]})
        # Close at value 22.5 (SL): loss since 22.5 < net_debit 45.
        closed = await close_debit_spread(db, pos, reason="spread-sl", short_ltp=47.5, long_ltp=70.0)
        end = await _balance(db)
        return start, end, closed

    start, end, closed = asyncio.run(run())
    assert closed["realized_pnl"] < 0
    assert abs((end - start) - closed["realized_pnl"]) < 0.01


def test_close_writes_canonical_trade_fills_row():
    db = _DB()

    async def run():
        await db.paper_wallets.insert_one({"user_id": "u1", "balance": 500000.0,
                                           "initial_balance": 500000.0, "total_debited": 0, "total_credited": 0})
        res = await open_debit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                      spread=_spread(), lots=1, lot_size=50, mode="paper",
                                      idempotency_key="spread:s1:0915")
        pos = await db.strategy_positions.find_one({"id": res["id"]})
        closed = await close_debit_spread(db, pos, reason="spread-tp", short_ltp=17.5, long_ltp=90.0)
        return closed

    closed = asyncio.run(run())
    fills = db.trade_fills.docs
    assert len(fills) == 1
    row = fills[0]
    assert row["action"] == "CLOSE"
    assert row["structure"] == "debit_spread"
    assert row["strategy_id"] == "s1" and row["user_id"] == "u1"
    assert abs(row["realized_pnl"] - closed["realized_pnl"]) < 0.01
    assert row["created_at"] and row["charges"] >= 0
    assert abs((row["realized_pnl"] + row["charges"]) - row["gross_pnl"]) < 0.01


def test_double_close_skips():
    db = _DB()

    async def run():
        await db.paper_wallets.insert_one({"user_id": "u1", "balance": 500000.0,
                                           "initial_balance": 500000.0, "total_debited": 0, "total_credited": 0})
        res = await open_debit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                      spread=_spread(), lots=1, lot_size=50, mode="paper",
                                      idempotency_key="spread:s1:0915")
        pos = await db.strategy_positions.find_one({"id": res["id"]})
        c1 = await close_debit_spread(db, pos, reason="spread-tp", short_ltp=17.5, long_ltp=90.0)
        c2 = await close_debit_spread(db, pos, reason="spread-tp", short_ltp=17.5, long_ltp=90.0)
        return c1, c2

    c1, c2 = asyncio.run(run())
    assert c1["ok"] and not c2["ok"] and c2["status"] == "SKIPPED"


def test_paper_only_guard():
    db = _DB()
    res = asyncio.run(open_debit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                        spread=_spread(), lots=1, lot_size=50, mode="live",
                                        idempotency_key="k"))
    assert not res["ok"] and res["status"] == "SKIPPED"
