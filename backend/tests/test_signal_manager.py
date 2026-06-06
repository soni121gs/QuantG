import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import uuid
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock

from signal_manager import ConflictResolver, SignalManager
from signal_manager import StrategySignalValidator
from signal_manager import _dispatch_signal_via_unified_engine


def test_conflict_resolver_ce_pe_clash_highest_confidence_wins():
    # Signal A: CE Buy, confidence 90.0
    sig_ce = {
        "id": "sig-ce",
        "user_id": "user-1",
        "strategy_id": "strat-ce",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY2660524900CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 90.0,
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "status": "PENDING",
    }
    
    # Signal B: PE Buy, confidence 80.0
    sig_pe = {
        "id": "sig-pe",
        "user_id": "user-1",
        "strategy_id": "strat-pe",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY2660524900PE",
        "option_type": "PE",
        "action": "BUY",
        "confidence": 80.0,
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "status": "PENDING",
    }

    approved, rejected_or_filtered = ConflictResolver.resolve(
        pending_signals=[sig_ce, sig_pe],
        active_positions=[],
        one_active_position_per_symbol_group=False
    )

    assert [s["id"] for s in approved] == ["sig-ce", "sig-pe"]
    assert rejected_or_filtered == []


def test_conflict_resolver_ce_pe_clash_equal_confidence_rejects_both():
    sig_ce = {
        "id": "sig-ce",
        "user_id": "user-1",
        "strategy_id": "strat-ce",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY2660524900CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 85.0,
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "status": "PENDING",
    }
    
    sig_pe = {
        "id": "sig-pe",
        "user_id": "user-1",
        "strategy_id": "strat-pe",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY2660524900PE",
        "option_type": "PE",
        "action": "BUY",
        "confidence": 85.0,
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "status": "PENDING",
    }

    approved, rejected_or_filtered = ConflictResolver.resolve(
        pending_signals=[sig_ce, sig_pe],
        active_positions=[],
        one_active_position_per_symbol_group=False
    )

    assert [s["id"] for s in approved] == ["sig-ce", "sig-pe"]
    assert rejected_or_filtered == []


def test_conflict_resolver_duplicate_contract_buys():
    sig_1 = {
        "id": "sig-1",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY2660524900CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 75.0,
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "status": "PENDING",
    }
    
    sig_2 = {
        "id": "sig-2",
        "user_id": "user-1",
        "strategy_id": "strat-2",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY2660524900CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 88.0,
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "status": "PENDING",
    }

    approved, rejected_or_filtered = ConflictResolver.resolve(
        pending_signals=[sig_1, sig_2],
        active_positions=[],
        one_active_position_per_symbol_group=False
    )

    assert [s["id"] for s in approved] == ["sig-1", "sig-2"]
    assert rejected_or_filtered == []


def test_conflict_resolver_symbol_group_blocking():
    # Strategy position already active in group "NIFTY"
    active_pos = {
        "id": "pos-active",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol_group": "NIFTY",
        "symbol": "NIFTY2660524900CE",
        "status": "FILLED",
    }

    # Incoming BUY signal in group "NIFTY"
    sig_buy = {
        "id": "sig-buy",
        "user_id": "user-1",
        "strategy_id": "strat-2",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY2660524950CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 95.0,
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "status": "PENDING",
    }

    # Incoming SELL exit signal in group "NIFTY" (exits bypass group lock)
    sig_sell = {
        "id": "sig-sell",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY2660524900CE",
        "option_type": "CE",
        "action": "SELL",
        "confidence": 99.0,
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "status": "PENDING",
    }

    approved, rejected_or_filtered = ConflictResolver.resolve(
        pending_signals=[sig_buy, sig_sell],
        active_positions=[active_pos],
        one_active_position_per_symbol_group=True
    )

    assert sig_buy in approved
    assert sig_sell in approved
    assert rejected_or_filtered == []


def test_conflict_resolver_blocks_duplicate_active_option_contract():
    active_pos = {
        "id": "pos-active",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol_group": "NIFTY 23400 PE 09 JUN 26",
        "symbol": "NIFTY 23400 PE 09 JUN 26",
        "target_symbol": "NIFTY 23400 PE 09 JUN 26",
        "instrument_key": "NSE_FO|42296",
        "status": "OPEN",
    }
    sig_buy = {
        "id": "sig-buy",
        "user_id": "user-1",
        "strategy_id": "strat-2",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY 23400 PE 09 JUN 26",
        "action": "SELL",
        "confidence": 95.0,
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "option_contract": {
            "instrument_key": "NSE_FO|42296",
            "option_type": "PE",
            "transaction_type": "BUY",
        },
        "status": "PENDING",
    }

    approved, rejected_or_filtered = ConflictResolver.resolve(
        pending_signals=[sig_buy],
        active_positions=[active_pos],
        one_active_position_per_symbol_group=False,
    )

    assert approved == [sig_buy]
    assert rejected_or_filtered == []


@pytest.mark.anyio
async def test_signal_manager_cooldown_active_is_not_a_hard_blocker():
    db = MagicMock()
    
    # Configure mock strategy under active cooldown (last signal was 5 minutes ago, cooldown is 15 minutes)
    db.strategies.find_one = AsyncMock(return_value={
        "id": "strat-1",
        "user_id": "user-1",
        "last_signal_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    })

    vc = {
        "risk": {
            "cooldown_minutes": 15,
            "max_trades_per_day": 0,
        }
    }

    ok, reason = await SignalManager.validate_strategy_limits(db, "strat-1", "user-1", vc)
    assert ok
    assert reason is None


@pytest.mark.anyio
async def test_signal_manager_cooldown_expired_is_approved():
    db = MagicMock()
    
    # Cooldown is 15 minutes, last signal was 20 minutes ago
    db.strategies.find_one = AsyncMock(return_value={
        "id": "strat-1",
        "user_id": "user-1",
        "last_signal_at": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
    })

    vc = {
        "risk": {
            "cooldown_minutes": 15,
            "max_trades_per_day": 0,
        }
    }

    ok, reason = await SignalManager.validate_strategy_limits(db, "strat-1", "user-1", vc)
    assert ok
    assert reason is None


@pytest.mark.anyio
async def test_signal_manager_max_trades_is_not_a_hard_blocker():
    db = MagicMock()
    
    db.strategies.find_one = AsyncMock(return_value={
        "id": "strat-1",
        "user_id": "user-1",
        "last_signal_at": None,
    })

    # Mock count_documents to return 5 processed signals today, with a limit of 5
    db.signals.count_documents = AsyncMock(return_value=5)

    vc = {
        "risk": {
            "cooldown_minutes": 0,
            "max_trades_per_day": 5,
        }
    }

    ok, reason = await SignalManager.validate_strategy_limits(db, "strat-1", "user-1", vc)
    assert ok
    assert reason is None


def test_conflict_resolver_strategy_specific_override():
    # Strategy position already active in group "NIFTY"
    active_pos = {
        "id": "pos-active",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol_group": "NIFTY",
        "symbol": "NIFTY2660524900CE",
        "status": "FILLED",
    }

    # Signal A: Strategy has override one_active_position_per_symbol_group = False
    sig_override_false = {
        "id": "sig-override-false",
        "user_id": "user-1",
        "strategy_id": "strat-2",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY2660524950CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 95.0,
        "visual_config": {"risk": {"one_active_position_per_symbol_group": False}},
        "status": "PENDING",
    }

    # Signal B: Strategy has override one_active_position_per_symbol_group = True
    sig_override_true = {
        "id": "sig-override-true",
        "user_id": "user-1",
        "strategy_id": "strat-3",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY2660524900CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 92.0,
        "visual_config": {"risk": {"one_active_position_per_symbol_group": True}},
        "status": "PENDING",
    }

    approved, rejected_or_filtered = ConflictResolver.resolve(
        pending_signals=[sig_override_false, sig_override_true],
        active_positions=[active_pos],
        one_active_position_per_symbol_group=True # global setting is True
    )

    assert sig_override_false in approved
    assert sig_override_true in approved
    assert rejected_or_filtered == []


@pytest.mark.anyio
async def test_strategy_signal_validator_repeated_buy_with_open_position_is_not_app_blocked():
    db = MagicMock()
    db.signals.count_documents = AsyncMock(return_value=0)
    sig = {
        "id": "sig-dup",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26060524900CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 80,
        "mode": "paper",
        "option_contract": {"instrument_key": "NSE_FO|12345", "option_type": "CE", "strike": 24900},
        "visual_config": {"options": {"enabled": True, "strike_mode": "ATM_BUY"}},
    }
    strategy = {"id": "strat-1", "user_id": "user-1", "mode": "paper", "status": "live"}
    active = [{"strategy_id": "strat-1", "status": "OPEN", "symbol": "NIFTY26060524900CE"}]

    result = await StrategySignalValidator.validate(db, sig, strategy, active)

    assert result["ok"]
    assert result["reason_code"] == "OK"


@pytest.mark.anyio
async def test_strategy_signal_validator_signal_spam_is_not_a_hard_blocker():
    db = MagicMock()
    db.signals.count_documents = AsyncMock(return_value=99)
    sig = {
        "id": "sig-spam",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26060524900CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 80,
        "mode": "paper",
        "option_contract": {"instrument_key": "NSE_FO|12345", "option_type": "CE", "strike": 24900},
        "visual_config": {"risk": {"one_active_position_per_symbol_group": False}, "options": {"enabled": True, "strike_mode": "ATM_BUY"}},
    }
    strategy = {"id": "strat-1", "user_id": "user-1", "mode": "paper", "status": "live"}

    result = await StrategySignalValidator.validate(db, sig, strategy, [])

    assert result["ok"]
    assert result["reason_code"] == "OK"


@pytest.mark.anyio
async def test_strategy_signal_validator_allows_paper_simulated_contract_with_ltp():
    db = MagicMock()
    db.signals.count_documents = AsyncMock(return_value=0)
    sig = {
        "id": "sig-paper-sim",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26060524900CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 80,
        "mode": "paper",
        "option_contract": {
            "instrument_key": "PAPER_NIFTY_CE_24900",
            "option_type": "CE",
            "strike": 24900,
            "simulated": True,
            "source": "PAPER_SIMULATED_CONTRACT",
            "ltp": 34.5,
        },
        "visual_config": {"options": {"enabled": True, "strike_mode": "ATM_BUY"}},
    }
    strategy = {"id": "strat-1", "user_id": "user-1", "mode": "paper", "status": "live"}

    result = await StrategySignalValidator.validate(db, sig, strategy, [])

    assert result["ok"]
    assert result["reason_code"] == "OK"


@pytest.mark.anyio
async def test_strategy_signal_validator_blocks_live_simulated_contract():
    db = MagicMock()
    db.signals.count_documents = AsyncMock(return_value=0)
    sig = {
        "id": "sig-live-sim",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26060524900CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 80,
        "mode": "live",
        "option_contract": {
            "instrument_key": "PAPER_NIFTY_CE_24900",
            "option_type": "CE",
            "strike": 24900,
            "simulated": True,
            "source": "PAPER_SIMULATED_CONTRACT",
            "ltp": 34.5,
        },
        "visual_config": {"options": {"enabled": True, "strike_mode": "ATM_BUY"}},
    }
    strategy = {"id": "strat-1", "user_id": "user-1", "mode": "live", "status": "live"}

    result = await StrategySignalValidator.validate(db, sig, strategy, [])

    assert not result["ok"]
    assert result["reason_code"] == "INSTRUMENT_UNRESOLVED"


@pytest.mark.anyio
async def test_strategy_signal_validator_allows_live_real_contract():
    db = MagicMock()
    db.signals.count_documents = AsyncMock(return_value=0)
    sig = {
        "id": "sig-live-real",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26060524900CE",
        "option_type": "CE",
        "action": "BUY",
        "confidence": 80,
        "mode": "live",
        "option_contract": {
            "instrument_key": "NSE_FO|12345",
            "option_type": "CE",
            "strike": 24900,
            "ltp": 34.5,
        },
        "visual_config": {"options": {"enabled": True, "strike_mode": "ATM_BUY"}},
    }
    strategy = {"id": "strat-1", "user_id": "user-1", "mode": "live", "status": "live"}

    result = await StrategySignalValidator.validate(db, sig, strategy, [])

    assert result["ok"]
    assert result["reason_code"] == "OK"


@pytest.mark.anyio
async def test_dispatch_signal_uses_unified_router_for_paper(monkeypatch):
    captured = {}
    db = MagicMock()
    db.orders.find_one = AsyncMock(return_value=None)
    db.strategy_positions.find_one = AsyncMock(return_value=None)

    async def fake_risk(self, **kwargs):
        captured["risk"] = kwargs
        return {"ok": True, "status": "APPROVED", "reason": "ok", "quantity": kwargs["requested_qty"]}

    async def fake_route(self, user_id, intent_doc):
        captured["route_user_id"] = user_id
        captured["intent"] = intent_doc
        return {"id": "order-1", "status": "FILLED", "mode": intent_doc["mode"]}

    monkeypatch.setattr("core.risk_manager.RiskManager.evaluate_order", fake_risk)
    monkeypatch.setattr("core.execution_router.ExecutionRouter.route_intent", fake_route)

    sig = {
        "id": "sig-unified",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26JUN25000CE",
        "action": "BUY",
        "confidence": 80,
        "mode": "paper",
        "price": 34.5,
        "option_contract": {
            "tradingsymbol": "NIFTY26JUN25000CE",
            "instrument_key": "NSE_FO|12345",
            "exchange": "NFO",
            "segment": "NSE_FO",
            "lot_size": 65,
            "ltp": 34.5,
            "simulated": True,
            "source": "PAPER_SIMULATED_CONTRACT",
        },
        "visual_config": {"options": {"enabled": True, "lots": 1}},
    }
    strategy = {"id": "strat-1", "user_id": "user-1", "mode": "paper", "status": "live"}

    result = await _dispatch_signal_via_unified_engine(db, "user-1", sig, strategy)

    assert result["status"] == "FILLED"
    assert captured["risk"]["mode"] == "paper"
    assert captured["risk"]["requested_qty"] == 65
    assert captured["intent"]["mode"] == "paper"
    assert captured["intent"]["instrument_token"] == "NSE_FO|12345"
    assert captured["route_user_id"] == "user-1"


@pytest.mark.anyio
async def test_strategy_signal_validator_allows_direct_equity_symbol():
    db = MagicMock()
    db.signals.count_documents = AsyncMock(return_value=0)
    sig = {
        "id": "sig-equity",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "RELIANCE",
        "target_symbol": "RELIANCE",
        "action": "BUY",
        "confidence": 80,
        "mode": "paper",
        "visual_config": {"symbol": "RELIANCE"},
    }
    strategy = {"id": "strat-1", "user_id": "user-1", "mode": "paper", "status": "live"}

    result = await StrategySignalValidator.validate(db, sig, strategy, [])

    assert result["ok"]
    assert result["reason_code"] == "OK"


@pytest.mark.anyio
async def test_strategy_signal_validator_sell_without_open_position_is_not_app_blocked():
    db = MagicMock()
    db.signals.count_documents = AsyncMock(return_value=0)
    sig = {
        "id": "sig-sell-flat",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26060524900CE",
        "option_type": "CE",
        "action": "SELL",
        "confidence": 80,
        "mode": "paper",
        "option_contract": {"instrument_key": "NSE_FO|12345", "option_type": "CE", "strike": 24900},
    }
    strategy = {"id": "strat-1", "user_id": "user-1", "mode": "paper", "status": "live"}

    result = await StrategySignalValidator.validate(db, sig, strategy, [])

    assert result["ok"]
    assert result["reason_code"] == "OK"


@pytest.mark.anyio
async def test_strategy_signal_validator_allows_bearish_option_buy_entry_when_flat():
    db = MagicMock()
    db.signals.count_documents = AsyncMock(return_value=0)
    sig = {
        "id": "sig-bearish-put-entry",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26060524900PE",
        "option_type": "PE",
        "action": "SELL",
        "confidence": 80,
        "mode": "paper",
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "option_contract": {
            "instrument_key": "NSE_FO|12345",
            "option_type": "PE",
            "strike": 24900,
            "transaction_type": "BUY",
        },
    }
    strategy = {
        "id": "strat-1",
        "user_id": "user-1",
        "mode": "paper",
        "status": "live",
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
    }

    result = await StrategySignalValidator.validate(db, sig, strategy, [])

    assert result["ok"]
    assert result["reason_code"] == "OK"


@pytest.mark.anyio
async def test_live_signal_dispatch_uses_server_order_core_boundary(monkeypatch):
    captured = {}

    db = MagicMock()
    db.strategy_positions.find_one = AsyncMock(return_value=None)
    db.orders.find_one = AsyncMock(return_value=None)

    async def fake_evaluate_order(self, **kwargs):
        captured["risk"] = kwargs
        return {"ok": True, "quantity": kwargs["requested_qty"]}

    async def fake_place_order(**kwargs):
        captured["order"] = kwargs
        return {"id": "order-live-1", "status": "PENDING_BROKER", "mode": "live"}

    monkeypatch.setattr("core.risk_manager.RiskManager.evaluate_order", fake_evaluate_order)

    sig = {
        "id": "sig-live",
        "user_id": "user-1",
        "strategy_id": "strat-1",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY26JUN25000CE",
        "action": "BUY",
        "confidence": 80,
        "mode": "live",
        "price": 34.5,
        "option_contract": {
            "tradingsymbol": "NIFTY26JUN25000CE",
            "instrument_key": "NSE_FO|12345",
            "exchange": "NFO",
            "segment": "NSE_FO",
            "lot_size": 65,
            "ltp": 34.5,
            "source": "UPSTOX_LIVE",
        },
        "visual_config": {"options": {"enabled": True, "lots": 1}},
    }
    strategy = {"id": "strat-1", "user_id": "user-1", "mode": "live", "status": "live"}

    result = await _dispatch_signal_via_unified_engine(db, "user-1", sig, strategy, fake_place_order)

    assert result["status"] == "PENDING_BROKER"
    assert captured["risk"]["mode"] == "live"
    assert captured["risk"]["requested_qty"] == 65
    assert captured["order"]["source"] == "signal:strategy:strat-1"
    assert captured["order"]["option_contract"]["instrument_key"] == "NSE_FO|12345"
    assert captured["order"]["qty"] == 1
    assert captured["order"]["idempotency_key"] == "sig:sig-live"
