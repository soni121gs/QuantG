import asyncio
from types import SimpleNamespace

import pytest

from core.spread_builder import cap_lots_by_risk
from core.spread_lifecycle import open_credit_spread, close_credit_spread
from core.paper_broker import PaperWallet
from core.knowledge_layer import promotion_stage
from core.hermes_diagnostics.probes_execution import exit_reason_mix
from core.hermes_diagnostics.probes_strategy import geometry_epoch
from test_spread_lifecycle import _DB, _spread, _balance
from scripts.repair_audit_0909 import settlement_plan


def test_risk_ceiling_rejects_one_unaffordable_lot():
    assert cap_lots_by_risk(1, 450, 65, cap=8000) == 0
    assert cap_lots_by_risk(0, 10, 65, cap=8000) == 0
    assert cap_lots_by_risk(10, 100, 65, cap=15000) == 2


def test_missing_oos_cannot_promote_even_with_profitable_paper_and_quality():
    result = promotion_stage("scale_candidate", None, 100, 10000, 100)
    assert result["stage"] != "candidate_live"
    assert any("MISSING" in b for b in result["blockers"])


def test_protective_exits_do_not_trigger_dead_engine_alarm():
    ctx = SimpleNamespace(closed_today=[
        {"structure": "credit_spread", "exit_reason": "profit-protect"},
        {"structure": "credit_spread", "exit_reason": "profit-protect"},
        {"structure": "credit_spread", "exit_reason": "profit-protect"},
        {"structure": "debit_spread", "exit_reason": "debit-payback-tp"},
    ])
    assert asyncio.run(exit_reason_mix(ctx)) == []


def test_epoch_uses_latest_material_change():
    assert geometry_epoch({"geometry_changed_at": "2026-07-30T00:00:00Z",
                           "visual_config": {"options": {"geometry_changed_at": "2026-08-31T07:16:54Z"}}}) == "2026-08-31T07:16:54+00:00"


def test_settlement_repair_accounts_for_exercise_tax_and_is_stable():
    pos = {"id": "example", "settlement_source": "intrinsic", "settlement_legs": {"short": 264.9, "long": 64.9},
           "quantity": 65, "structure": "credit_spread", "expiry": "2026-09-08",
           "net_credit": 34.63, "entry_charges": 30, "realized_pnl": -10886.91}
    plan = settlement_plan(pos)
    assert plan["gross_pnl"] == -10749.05
    assert plan["charges"] == 36.33
    assert plan["realized_pnl"] == -10785.38
    assert plan["delta"] == 101.53
    assert settlement_plan({**pos, "realized_pnl": plan["realized_pnl"]})["delta"] == 0


def test_entry_rejects_over_budget_before_wallet_mutation():
    async def run():
        db = _DB()
        db.strategies.docs[0]["required_capital"] = 8000
        result = await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                         spread=_spread(width=500), lots=1, lot_size=65,
                                         idempotency_key="overbudget")
        assert not result["ok"]
        assert not db.paper_wallets.docs
        assert not db.strategy_positions.docs
        assert not db.strategy_position_locks.docs
    asyncio.run(run())


def test_nearby_strike_does_not_bypass_correlated_exposure():
    async def run():
        db = _DB()
        args = dict(user_id="u1", strategy_id="s1", underlying="NIFTY", lots=1, lot_size=65)
        assert (await open_credit_spread(db, spread=_spread(), idempotency_key="first", **args))["ok"]
        another = _spread()
        another["short_leg"]["strike"] += 50
        result = await open_credit_spread(db, spread=another, idempotency_key="second", **args)
        assert result["reason_code"] == "SPREAD_CORRELATED_EXPOSURE"
        assert len(db.strategy_positions.docs) == 1
    asyncio.run(run())


def test_expiry_intrinsic_is_not_slipped_and_wallet_matches_pnl():
    async def run():
        db = _DB()
        await open_credit_spread(db, user_id="u1", strategy_id="s1", underlying="NIFTY",
                                 spread=_spread(), lots=1, lot_size=65, idempotency_key="settle")
        pos = dict(db.strategy_positions.docs[0])
        await close_credit_spread(db, pos, reason="expiry-settlement", short_ltp=250, long_ltp=150)
        closed = db.strategy_positions.docs[0]
        assert closed["exit_value"] == 100
        assert closed["gross_pnl"] == pytest.approx((pos["net_credit"] - 100) * 65)
        assert await _balance(db) - 500000 == pytest.approx(closed["realized_pnl"])
    asyncio.run(run())


def test_terminal_margin_release_is_idempotent_and_preserves_open_book():
    async def run():
        db = _DB()
        wallet = PaperWallet(db)
        await wallet.get_or_initialize("u1")
        for pid, status in [("cancelled", "CANCELLED"), ("open", "OPEN")]:
            await db.strategy_positions.insert_one({"id": pid, "user_id": "u1", "mode": "paper", "status": status})
            await wallet.block_margin("u1", 1000, pid)
        await wallet.release_terminal_margin("u1")
        await wallet.release_terminal_margin("u1")
        row = await wallet.get_or_initialize("u1")
        assert row["blocked_margin"] == 1000
        assert row["balance"] == 500000
        assert (await db.paper_margin_blocks.find_one({"position_id": "open"}))["status"] == "BLOCKED"
    asyncio.run(run())
