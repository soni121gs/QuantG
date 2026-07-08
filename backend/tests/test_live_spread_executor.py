"""Live spread execution path: gates, leg ordering, unwind-on-failure,
partial-close resume, and accounting parity with the paper path.

Uses a fake place/details pair — no broker, no network.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.live_spread_executor as lse
from core.spread_lifecycle import open_credit_spread, close_credit_spread
from test_spread_lifecycle import _DB


def _spread():
    return {
        "ok": True,
        "direction": "bullish",
        "option_type": "PE",
        "net_credit": 20.0,
        "width_points": 50.0,
        "max_loss": 30.0,
        "short_leg": {"role": "short", "side": "SELL", "strike": 25500.0, "premium": 60.0,
                      "instrument_key": "NSE_FO|11111", "option_type": "PE", "expiry": "2026-07-09"},
        "long_leg": {"role": "long", "side": "BUY", "strike": 25450.0, "premium": 40.0,
                     "instrument_key": "NSE_FO|22222", "option_type": "PE", "expiry": "2026-07-09"},
    }


class _FakeBroker:
    """Scriptable place/details pair. fills maps instrument_key+side -> avg price
    or Exception to raise on placement; details always reports complete."""

    def __init__(self, fills):
        self.fills = fills
        self.placed = []          # (instrument_key, side, qty, tag)
        self._avg_by_order = {}
        self._n = 0

    async def place(self, user_id, *, instrument_token, side, quantity,
                    order_type, product, tag, **kw):
        key = f"{instrument_token}:{side}"
        val = self.fills.get(key)
        self.placed.append((instrument_token, side, quantity, tag))
        if isinstance(val, Exception):
            raise val
        self._n += 1
        oid = f"UPX{self._n:04d}"
        self._avg_by_order[oid] = float(val)
        return {"ok": True, "broker_order_id": oid}

    async def details(self, user_id, order_id):
        return {"data": {"status": "complete", "average_price": self._avg_by_order[order_id]}}


def _arm(env=True):
    os.environ["CORE_ENGINE_LIVE_ENABLED"] = "true" if env else "false"
    os.environ["LIVE_SPREADS_ENABLED"] = "true" if env else "false"


def _configure(broker):
    lse.configure(place_order_fn=broker.place, order_details_fn=broker.details)


def teardown_function(_fn):
    os.environ["CORE_ENGINE_LIVE_ENABLED"] = "false"
    os.environ["LIVE_SPREADS_ENABLED"] = "false"
    lse._place_order_fn = None
    lse._order_details_fn = None


def test_live_entry_blocked_when_flags_off():
    async def run():
        db = _DB()
        _arm(False)
        _configure(_FakeBroker({}))
        res = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                       spread=_spread(), lots=1, lot_size=65, mode="live",
                                       idempotency_key="k1")
        assert res["ok"] is False
        assert "CORE_ENGINE_LIVE_ENABLED" in res["reason"]
        assert db.strategy_positions.docs == []
    asyncio.run(run())


def test_live_entry_blocked_when_not_configured():
    async def run():
        db = _DB()
        _arm(True)  # flags on but no executor wiring
        res = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                       spread=_spread(), lots=1, lot_size=65, mode="live",
                                       idempotency_key="k1")
        assert res["ok"] is False
        assert "not configured" in res["reason"]
    asyncio.run(run())


def test_live_entry_buys_long_first_and_reprices_off_real_fills():
    async def run():
        db = _DB()
        _arm(True)
        broker = _FakeBroker({"NSE_FO|22222:BUY": 41.0, "NSE_FO|11111:SELL": 58.5})
        _configure(broker)
        res = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                       spread=_spread(), lots=1, lot_size=65, mode="live",
                                       idempotency_key="k1")
        assert res["ok"] is True
        # leg ordering: protective long BUY first, short SELL second
        assert [(p[0], p[1]) for p in broker.placed] == [
            ("NSE_FO|22222", "BUY"), ("NSE_FO|11111", "SELL")]
        pos = db.strategy_positions.docs[0]
        assert pos["mode"] == "live"
        # repriced off real fills: 58.5 - 41.0 = 17.5 credit, max_loss = 50 - 17.5
        assert pos["net_credit"] == 17.5
        assert pos["max_loss"] == 32.5
        assert pos["entry_broker_order_ids"] == {"long": "UPX0001", "short": "UPX0002"}
        # no paper wallet involvement
        assert db.paper_wallets.docs == []
        assert db.paper_margin_blocks.docs == []
        # both leg audit orders carry the broker
        assert all(o["broker"] == "upstox" for o in db.orders.docs)
    asyncio.run(run())


def test_live_entry_unwinds_long_when_short_leg_fails():
    async def run():
        db = _DB()
        _arm(True)
        broker = _FakeBroker({"NSE_FO|22222:BUY": 41.0,
                              "NSE_FO|11111:SELL": RuntimeError("margin insufficient"),
                              "NSE_FO|22222:SELL": 40.5})
        _configure(broker)
        res = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                       spread=_spread(), lots=1, lot_size=65, mode="live",
                                       idempotency_key="k1")
        assert res["ok"] is False
        assert "short_entry" in res["reason"]
        # long bought, short attempted, long unwound — never naked short, no position doc
        sides = [(p[0], p[1]) for p in broker.placed]
        assert sides == [("NSE_FO|22222", "BUY"), ("NSE_FO|11111", "SELL"), ("NSE_FO|22222", "SELL")]
        assert db.strategy_positions.docs == []
    asyncio.run(run())


def test_live_entry_orphan_incident_when_unwind_also_fails():
    async def run():
        db = _DB()
        _arm(True)
        broker = _FakeBroker({"NSE_FO|22222:BUY": 41.0,
                              "NSE_FO|11111:SELL": RuntimeError("rejected"),
                              "NSE_FO|22222:SELL": RuntimeError("exchange closed")})
        _configure(broker)
        res = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                       spread=_spread(), lots=1, lot_size=65, mode="live",
                                       idempotency_key="k1")
        assert res["ok"] is False
        assert len(db.live_spread_incidents.docs) == 1
        assert db.live_spread_incidents.docs[0]["kind"] == "ORPHAN_LONG_LEG"
    asyncio.run(run())


async def _open_live_position(db, broker):
    _arm(True)
    _configure(broker)
    res = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                   spread=_spread(), lots=1, lot_size=65, mode="live",
                                   idempotency_key="k1")
    assert res["ok"] is True
    return await db.strategy_positions.find_one({"id": res["id"]})


def test_live_close_buys_back_short_first_and_realizes_real_pnl():
    async def run():
        db = _DB()
        broker = _FakeBroker({"NSE_FO|22222:BUY": 41.0, "NSE_FO|11111:SELL": 58.5,
                              "NSE_FO|11111:BUY": 30.0, "NSE_FO|22222:SELL": 25.0})
        pos = await _open_live_position(db, broker)
        broker.placed.clear()
        closed = await close_credit_spread(db, pos, reason="spread-tp",
                                           short_ltp=999.0, long_ltp=999.0)  # LTPs must be ignored
        assert closed["ok"] is True
        # exit leg ordering: BUY back short first, then SELL long
        assert [(p[0], p[1]) for p in broker.placed] == [
            ("NSE_FO|11111", "BUY"), ("NSE_FO|22222", "SELL")]
        # close value from REAL fills: 30 - 25 = 5; entry credit 17.5 -> gross (17.5-5)*65
        assert closed["exit_value"] == 5.0
        assert closed["gross_pnl"] == round((17.5 - 5.0) * 65, 2)
        final = await db.strategy_positions.find_one({"id": pos["id"]})
        assert final["status"] == "CLOSED"
        # canonical rows written with mode=live
        assert any(t.get("mode") == "live" for t in db.trade_fills.docs)
        assert db.paper_wallets.docs == []  # wallet untouched on live close
    asyncio.run(run())


def test_live_close_partial_resumes_without_rebuying_short():
    async def run():
        db = _DB()
        broker = _FakeBroker({"NSE_FO|22222:BUY": 41.0, "NSE_FO|11111:SELL": 58.5,
                              "NSE_FO|11111:BUY": 30.0,
                              "NSE_FO|22222:SELL": RuntimeError("throttled")})
        pos = await _open_live_position(db, broker)
        broker.placed.clear()
        first = await close_credit_spread(db, pos, reason="spread-sl",
                                          short_ltp=0.0, long_ltp=0.0)
        assert first["ok"] is False and first["stage"] == "long_close"
        mid = await db.strategy_positions.find_one({"id": pos["id"]})
        assert mid["status"] == "EXITING"  # stays claimed; state persisted
        assert mid["live_close_state"]["short_closed"] is True
        assert len(db.live_spread_incidents.docs) == 1

        # retry (monitor auto-revert path): long sell now succeeds
        broker.fills["NSE_FO|22222:SELL"] = 25.0
        broker.placed.clear()
        mid["status"] = "OPEN"
        await db.strategy_positions.update_one({"id": pos["id"]}, {"$set": {"status": "OPEN"}})
        second = await close_credit_spread(db, mid, reason="spread-sl",
                                           short_ltp=0.0, long_ltp=0.0)
        assert second["ok"] is True
        # only the long leg was placed on retry — the short was NOT re-bought
        assert [(p[0], p[1]) for p in broker.placed] == [("NSE_FO|22222", "SELL")]
        final = await db.strategy_positions.find_one({"id": pos["id"]})
        assert final["status"] == "CLOSED"
    asyncio.run(run())


def test_live_close_short_failure_reverts_to_open_for_retry():
    async def run():
        db = _DB()
        broker = _FakeBroker({"NSE_FO|22222:BUY": 41.0, "NSE_FO|11111:SELL": 58.5,
                              "NSE_FO|11111:BUY": RuntimeError("no quote")})
        pos = await _open_live_position(db, broker)
        res = await close_credit_spread(db, pos, reason="spread-tp",
                                        short_ltp=0.0, long_ltp=0.0)
        assert res["ok"] is False and res["stage"] == "short_close"
        final = await db.strategy_positions.find_one({"id": pos["id"]})
        assert final["status"] == "OPEN"  # handed back to the monitor
        assert "no quote" in str(final.get("close_error"))
    asyncio.run(run())


def test_paper_mode_untouched_by_live_wiring():
    async def run():
        db = _DB()
        await db.paper_wallets.insert_one({"user_id": "u1", "balance": 500000.0,
                                           "total_debited": 0.0, "total_credited": 0.0})
        _arm(True)
        _configure(_FakeBroker({}))  # live wiring present but mode=paper must not use it
        res = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                       spread=_spread(), lots=1, lot_size=65, mode="paper",
                                       idempotency_key="k1")
        assert res["ok"] is True
        # Paper applies adverse slippage to the QUOTED premiums (not the live broker
        # fills, which the wiring would have used) — so net credit is below the 20.0 mid.
        assert res["net_credit"] < 20.0
        assert db.strategy_positions.docs[0]["mode"] == "paper"
        assert len(db.paper_margin_blocks.docs) == 1  # paper margin block still applies
    asyncio.run(run())
