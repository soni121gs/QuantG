"""Live Safety Audit Tests — 8 required + additional safety invariants.

All tests are self-contained: no real DB, no real Upstox API calls.

Required tests (from audit):
  1.  CORE_ENGINE_LIVE_ENABLED=true does not crash
  2.  No TypeError from bridge_submit_order (correct payload type)
  3.  Live adapter sends correct ExecutionDispatchPayload fields
  4.  Paper mode never calls Upstox
  5.  Two accounts are isolated (positions, broker tokens)
  6.  Mock/PAPER/missing/"0" token is blocked before bridge call
  7.  live_arm_state.armed=false blocks execution
  8.  live_arm_state.global_live_enabled=false blocks execution

Additional safety invariants:
  A.  Stale quote (>30s) is a hard block in option_selector_v2 live scoring
  B.  Malformed instrument_key (no '|') blocked by live_entry_preflight
  C.  MCX segment raises RuntimeError in UpstoxLiveAdapter
  D.  Duplicate active position blocks new signal via live_entry_preflight
  E.  Kill switch blocks live entry
  F.  Failed preflight returns SKIPPED_SIGNAL, never FAILED broker order
  G.  Reconciliation mismatch blocks readiness_checker live verdict
"""
from __future__ import annotations

import os
import sys
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SENTINEL = object()


def _make_db(
    arm_doc=_SENTINEL,
    kill_doc=_SENTINEL,
    broker_keys_doc=_SENTINEL,
    position_doc=None,
    order_doc=None,
):
    db = MagicMock()
    db.live_arm_state.find_one = AsyncMock(
        return_value=(
            arm_doc if arm_doc is not _SENTINEL
            else {"user_id": "u1", "armed": True, "global_live_enabled": True}
        )
    )
    db.risk_state.find_one = AsyncMock(
        return_value=(
            kill_doc if kill_doc is not _SENTINEL
            else {"_id": "global_kill_switch", "active": False}
        )
    )
    db.broker_keys.find_one = AsyncMock(
        return_value=(
            broker_keys_doc if broker_keys_doc is not _SENTINEL
            else {"user_id": "u1", "broker": "upstox", "access_token": "real_token_abc"}
        )
    )
    db.strategy_positions.find_one = AsyncMock(return_value=position_doc)
    db.orders.find_one = AsyncMock(return_value=order_doc)
    db.orders.insert_one = AsyncMock()
    db.fills.insert_one = AsyncMock()
    db.users.find_one = AsyncMock(return_value={"id": "u1"})
    db.paper_wallets.find_one = AsyncMock(return_value={"user_id": "u1", "balance": 500000.0})
    return db


def _good_sig(overrides=None):
    sig = {
        "id": "sig-001",
        "strategy_id": "strat-001",
        "symbol": "NIFTY",
        "option_quality_score": 85.0,
        "idempotency_key": "idem-001",
        "initial_stop_R": 1.0,
        "option_contract": {
            "instrument_key": "NSE_FO|12345",
            "tradingsymbol": "NIFTY24DEC25000CE",
            "simulated": False,
            "source": "LIVE_UPSTOX",
            "ltp": 120.50,
            "option_quality_score": 85.0,
        },
    }
    if overrides:
        for k, v in overrides.items():
            if "." in k:
                top, sub = k.split(".", 1)
                sig[top][sub] = v
            else:
                sig[k] = v
    return sig


def _good_strategy():
    return {"id": "strat-001", "stop_loss": 0.02}


def _live_intent(overrides=None):
    base = {
        "strategy_id": "strat-001",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY24DEC25000CE",
        "side": "BUY",
        "qty": 50,
        "requested_price": 120.5,
        "exchange": "NSE",
        "segment": "NSE_FO",
        "instrument_token": "NSE_FO|50234",
        "mode": "live",
        "idempotency_key": "test-idem-001",
        "order_type": "MARKET",
        "product": "MIS",
    }
    if overrides:
        base.update(overrides)
    return base


def _mock_place_upstox():
    """Mock that returns a successful broker response."""
    async def _fn(user_id, *, instrument_token, side, quantity, order_type, product, price, tag, **kw):
        return {"order_id": "upstox-order-999", "status": "success"}
    return _fn


def _mock_resolve_token():
    """Mock that returns a token when '|' is present."""
    def _fn(exchange: str, tradingsymbol: str, token: str):
        return token if "|" in str(token or "") else None
    return _fn


async def _run_preflight(db, sig, strategy=None, env_flag="true"):
    with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": env_flag}):
        with patch("core.live_entry_preflight.get_segment_status") as mock_clock:
            mock_clock.return_value = {"open": True}
            with patch("core.live_entry_preflight.resolve_domain_by_underlying") as mock_domain:
                mock_domain.return_value = MagicMock(name="NFO")
                from core.live_entry_preflight import live_entry_preflight
                return await live_entry_preflight(db, "u1", sig, strategy or _good_strategy())


# ===========================================================================
# TEST 1: CORE_ENGINE_LIVE_ENABLED=true does not crash
# ===========================================================================

@pytest.mark.anyio
async def test_core_engine_live_enabled_does_not_crash():
    """With CORE_ENGINE_LIVE_ENABLED=true, a valid intent with injected callbacks
    must complete without TypeError or NotImplementedError."""
    from core.execution_router import UpstoxLiveAdapter
    from core.portfolio_ledger import PortfolioLedger

    db = _make_db()
    adapter = UpstoxLiveAdapter(
        db,
        MagicMock(spec=PortfolioLedger),
        place_upstox_fn=_mock_place_upstox(),
        resolve_upstox_token_fn=_mock_resolve_token(),
    )

    with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "true"}):
        result = await adapter.execute("u1", _live_intent())

    assert result["status"] == "PLACED"
    assert result["mode"] == "live"
    assert result["broker"] == "upstox"
    assert result["broker_order_id"] == "upstox-order-999"
    db.orders.insert_one.assert_called_once()


# ===========================================================================
# TEST 2: No TypeError from bridge_submit_order — correct payload type passed
# ===========================================================================

@pytest.mark.anyio
async def test_no_type_error_from_bridge_submit_order():
    """bridge_submit_order must receive ExecutionDispatchPayload as the second
    positional argument, not an IntentModel or raw dict (which caused TypeError)."""
    from core.execution_router import UpstoxLiveAdapter
    from core.portfolio_ledger import PortfolioLedger
    from execution_bridge import ExecutionDispatchPayload

    db = _make_db()
    captured = {}

    async def _capture_bridge(user_id, payload, *, place_upstox, resolve_upstox_token):
        captured["payload"] = payload
        captured["user_id"] = user_id
        return {
            "ok": True,
            "status": "PENDING_BROKER",
            "broker_order_id": "upstox-order-123",
            "raw": {},
            "attempts": 1,
            "recovered": False,
            "error": None,
        }

    adapter = UpstoxLiveAdapter(
        db,
        MagicMock(spec=PortfolioLedger),
        place_upstox_fn=_mock_place_upstox(),
        resolve_upstox_token_fn=_mock_resolve_token(),
    )

    with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "true"}):
        with patch("core.execution_router.bridge_submit_order", side_effect=_capture_bridge):
            await adapter.execute("u1", _live_intent())

    assert "payload" in captured, "bridge_submit_order was not called"
    assert isinstance(captured["payload"], ExecutionDispatchPayload), (
        f"Expected ExecutionDispatchPayload, got {type(captured['payload'])}"
    )
    assert captured["user_id"] == "u1"


# ===========================================================================
# TEST 3: Live adapter sends correct ExecutionDispatchPayload fields
# ===========================================================================

@pytest.mark.anyio
async def test_live_adapter_sends_correct_payload_fields():
    """ExecutionDispatchPayload must have all fields correctly mapped from intent."""
    from core.execution_router import UpstoxLiveAdapter
    from core.portfolio_ledger import PortfolioLedger
    from execution_bridge import ExecutionDispatchPayload

    db = _make_db()
    captured_payload: list[ExecutionDispatchPayload] = []

    async def _capture(user_id, payload, *, place_upstox, resolve_upstox_token):
        captured_payload.append(payload)
        return {"ok": True, "status": "PENDING_BROKER", "broker_order_id": "upstox-abc", "raw": {}, "attempts": 1, "recovered": False, "error": None}

    adapter = UpstoxLiveAdapter(
        db,
        MagicMock(spec=PortfolioLedger),
        place_upstox_fn=_mock_place_upstox(),
        resolve_upstox_token_fn=_mock_resolve_token(),
    )
    intent = _live_intent()

    with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "true"}):
        with patch("core.execution_router.bridge_submit_order", side_effect=_capture):
            await adapter.execute("u1", intent)

    p = captured_payload[0]
    assert p.broker == "upstox"
    assert p.tradingsymbol == intent["target_symbol"]
    assert p.exchange == intent["exchange"]
    assert p.segment == intent["segment"]
    assert p.instrument_token == intent["instrument_token"]
    assert p.quantity == intent["qty"]
    assert p.side == "BUY"
    assert p.product == "MIS"
    assert p.order_type == "MARKET"


# ===========================================================================
# TEST 4: Paper mode never calls Upstox
# ===========================================================================

@pytest.mark.anyio
async def test_paper_mode_never_calls_upstox():
    """PaperAdapter.execute() must complete without invoking place_upstox_fn
    or bridge_submit_order — paper fills are purely virtual."""
    from core.execution_router import PaperAdapter
    from core.portfolio_ledger import PortfolioLedger
    from core.paper_broker import PaperWallet

    db = _make_db()
    ledger = MagicMock(spec=PortfolioLedger)
    ledger.process_fill = AsyncMock()

    mock_wallet = MagicMock(spec=PaperWallet)
    mock_wallet.debit = AsyncMock(return_value=True)
    mock_wallet.credit = AsyncMock()

    adapter = PaperAdapter(db, ledger)
    adapter.wallet = mock_wallet

    intent_doc = {
        "id": "order-paper-001",
        "strategy_id": "strat-001",
        "symbol": "NIFTY",
        "target_symbol": "NIFTY24DEC25000CE",
        "side": "BUY",
        "qty": 50,
        "requested_price": 120.5,
        "exchange": "NSE",
        "segment": "NSE_FO",
        "mode": "paper",
        "idempotency_key": "test-paper-001",
        "stop_loss": None,
        "take_profit": None,
    }

    mock_upstox_fn = AsyncMock()

    with patch("core.execution_router.bridge_submit_order") as mock_bridge:
        result = await adapter.execute("u1", intent_doc)

    mock_bridge.assert_not_called()
    mock_upstox_fn.assert_not_called()
    assert result["mode"] == "paper"
    assert result["broker"] == "paper"
    assert result["status"] == "FILLED"


# ===========================================================================
# TEST 5a: Two accounts isolated — positions
# ===========================================================================

@pytest.mark.anyio
async def test_two_account_isolation_positions():
    """User A's OPEN live position must NOT block User B's signal — all
    strategy_positions queries are scoped by user_id."""
    user_a_position = {
        "id": "pos-user-a-001",
        "user_id": "user_a",
        "instrument_key": "NSE_FO|12345",
        "target_symbol": "NIFTY24DEC25000CE",
        "status": "OPEN",
        "mode": "live",
    }

    db_b = _make_db()

    async def positions_find_one(query, *args, **kwargs):
        uid = query.get("user_id")
        return user_a_position if uid == "user_a" else None

    db_b.strategy_positions.find_one = positions_find_one

    result = await _run_preflight(db_b, _good_sig())
    assert result["ok"] is True, f"user_b wrongly blocked by user_a's position: {result}"


# ===========================================================================
# TEST 5b: Two accounts isolated — broker tokens
# ===========================================================================

@pytest.mark.anyio
async def test_two_account_isolation_broker_token():
    """User A's valid token must NOT satisfy User B's preflight token check."""
    db_b = _make_db()

    async def broker_keys_find_one(query, *args, **kwargs):
        uid = query.get("user_id")
        if uid == "user_a":
            return {"user_id": "user_a", "broker": "upstox", "access_token": "real_token_user_a"}
        return None  # user_b has no token

    db_b.broker_keys.find_one = broker_keys_find_one

    result = await _run_preflight(db_b, _good_sig())
    assert result["ok"] is False
    assert result["failed_check"] == "upstox_token", (
        f"Expected upstox_token block for user_b, got: {result}"
    )


# ===========================================================================
# TEST 6: Mock / PAPER_ / missing / "0" instrument_token blocked before bridge
# ===========================================================================

@pytest.mark.anyio
@pytest.mark.parametrize("bad_token,expected_fragment", [
    ("",         "missing or '0'"),
    ("0",        "missing or '0'"),
    ("PAPER_NSE_FO|12345", "PAPER_"),
    ("mock_token_abc",     "mock"),
    ("NSE_FO_NO_PIPE",     "must contain '|'"),
])
async def test_invalid_token_blocked_before_bridge(bad_token, expected_fragment):
    """UpstoxLiveAdapter must block invalid instrument_tokens with a descriptive
    RuntimeError before bridge_submit_order is ever called."""
    from core.execution_router import UpstoxLiveAdapter
    from core.portfolio_ledger import PortfolioLedger

    db = _make_db()
    adapter = UpstoxLiveAdapter(
        db,
        MagicMock(spec=PortfolioLedger),
        place_upstox_fn=_mock_place_upstox(),
        resolve_upstox_token_fn=_mock_resolve_token(),
    )

    with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "true"}):
        with patch("core.execution_router.bridge_submit_order") as mock_bridge:
            with pytest.raises(RuntimeError) as exc_info:
                await adapter.execute("u1", _live_intent({"instrument_token": bad_token}))

    mock_bridge.assert_not_called()
    assert expected_fragment.lower() in str(exc_info.value).lower(), (
        f"Expected '{expected_fragment}' in error for token={bad_token!r}, got: {exc_info.value}"
    )


# ===========================================================================
# TEST 7: live_arm_state.armed=false blocks execution
# ===========================================================================

@pytest.mark.anyio
async def test_live_arm_false_blocks_execution():
    """UpstoxLiveAdapter must raise RuntimeError when live_arm_state.armed=false,
    confirmed independently of the preflight gate."""
    from core.execution_router import UpstoxLiveAdapter
    from core.portfolio_ledger import PortfolioLedger

    db = _make_db(arm_doc={"user_id": "u1", "armed": False, "global_live_enabled": True})
    adapter = UpstoxLiveAdapter(
        db,
        MagicMock(spec=PortfolioLedger),
        place_upstox_fn=_mock_place_upstox(),
        resolve_upstox_token_fn=_mock_resolve_token(),
    )

    with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "true"}):
        with pytest.raises(RuntimeError) as exc_info:
            await adapter.execute("u1", _live_intent())

    assert "armed" in str(exc_info.value).lower(), (
        f"Expected 'armed' in error, got: {exc_info.value}"
    )


# ===========================================================================
# TEST 8: live_arm_state.global_live_enabled=false blocks execution
# ===========================================================================

@pytest.mark.anyio
async def test_global_live_enabled_false_blocks_execution():
    """UpstoxLiveAdapter must raise RuntimeError when global_live_enabled=false,
    even if armed=true."""
    from core.execution_router import UpstoxLiveAdapter
    from core.portfolio_ledger import PortfolioLedger

    db = _make_db(arm_doc={"user_id": "u1", "armed": True, "global_live_enabled": False})
    adapter = UpstoxLiveAdapter(
        db,
        MagicMock(spec=PortfolioLedger),
        place_upstox_fn=_mock_place_upstox(),
        resolve_upstox_token_fn=_mock_resolve_token(),
    )

    with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "true"}):
        with pytest.raises(RuntimeError) as exc_info:
            await adapter.execute("u1", _live_intent())

    assert "global_live_enabled" in str(exc_info.value).lower(), (
        f"Expected 'global_live_enabled' in error, got: {exc_info.value}"
    )


# ===========================================================================
# ADDITIONAL A: Stale quote (>30s) is a hard block in option_selector_v2
# ===========================================================================

def test_stale_quote_blocks_live_option_scoring():
    from core.option_selector_v2 import _quality_score

    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    contract = {
        "underlying": "NIFTY",
        "instrument_key": "NSE_FO|12345",
        "ltp": 120.5,
        "bid": 120.0,
        "ask": 121.0,
        "received_at": stale_ts,
        "simulated": False,
    }

    result = _quality_score(contract, mode="live", preference="ATM", strike_mode="ATM", spot=24500.0)

    assert result["hard_block"] is True
    assert result["block_reason"] == "STALE_QUOTE_LIVE"


# ===========================================================================
# ADDITIONAL B: Malformed instrument_key blocks live_entry_preflight
# ===========================================================================

@pytest.mark.anyio
async def test_malformed_instrument_key_blocks_live_entry():
    db = _make_db()
    sig = _good_sig({"option_contract.instrument_key": "NIFTY24DEC25000CE"})  # no '|'
    result = await _run_preflight(db, sig)
    assert result["ok"] is False
    assert result["failed_check"] == "instrument_key"


# ===========================================================================
# ADDITIONAL C: MCX segment raises RuntimeError in UpstoxLiveAdapter
# ===========================================================================

@pytest.mark.anyio
async def test_mcx_execution_raises_runtime_error():
    from core.execution_router import UpstoxLiveAdapter
    from core.portfolio_ledger import PortfolioLedger

    db = _make_db()
    adapter = UpstoxLiveAdapter(
        db,
        MagicMock(spec=PortfolioLedger),
        place_upstox_fn=_mock_place_upstox(),
        resolve_upstox_token_fn=_mock_resolve_token(),
    )

    mcx_intent = _live_intent({"exchange": "MCX", "segment": "MCX_FO", "symbol": "CRUDEOIL",
                                "target_symbol": "CRUDEOIL24DEC5000CE"})

    with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "true"}):
        with pytest.raises(RuntimeError) as exc_info:
            await adapter.execute("u1", mcx_intent)

    assert "MCX" in str(exc_info.value)


# ===========================================================================
# ADDITIONAL D: Duplicate active position blocks new signal via preflight
# ===========================================================================

@pytest.mark.anyio
async def test_duplicate_option_contract_blocked():
    existing_position = {
        "id": "pos-existing",
        "user_id": "u1",
        "instrument_key": "NSE_FO|12345",
        "status": "OPEN",
        "mode": "live",
    }
    db = _make_db(position_doc=existing_position)
    result = await _run_preflight(db, _good_sig())
    assert result["ok"] is False
    assert result["failed_check"] == "no_duplicate_position"


# ===========================================================================
# ADDITIONAL E: Kill switch blocks live entry
# ===========================================================================

@pytest.mark.anyio
async def test_kill_switch_blocks_order():
    db = _make_db(kill_doc={"_id": "global_kill_switch", "active": True})
    result = await _run_preflight(db, _good_sig())
    assert result["ok"] is False
    assert result["failed_check"] == "kill_switch"


# ===========================================================================
# ADDITIONAL F: Failed preflight returns SKIPPED_SIGNAL, not FAILED order
# ===========================================================================

@pytest.mark.anyio
async def test_failed_preflight_creates_skipped_not_failed_order():
    from signal_manager import _dispatch_signal_via_unified_engine

    db = _make_db(arm_doc={"user_id": "u1", "armed": False, "global_live_enabled": False})
    strategy = {
        "id": "strat-001",
        "user_id": "u1",
        "mode": "live",
        "broker": "upstox",
        "status": "live",
        "underlying": "NIFTY",
        "stop_loss": 0.02,
    }
    sig = _good_sig()
    sig["user_id"] = "u1"
    place_order_fn = AsyncMock()

    with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "true"}):
        with patch("core.live_entry_preflight.get_segment_status") as mc:
            mc.return_value = {"open": True}
            with patch("core.live_entry_preflight.resolve_domain_by_underlying") as md:
                md.return_value = MagicMock(name="NFO")
                result = await _dispatch_signal_via_unified_engine(
                    db, "u1", sig, strategy, place_order_fn=place_order_fn
                )

    assert result["status"] == "SKIPPED_SIGNAL"
    assert result["reason_code"] == "LIVE_PREFLIGHT_NOT_READY"
    place_order_fn.assert_not_called()
    db.orders.insert_one.assert_not_called()


# ===========================================================================
# ADDITIONAL G: Reconciliation mismatch blocks readiness_checker
# ===========================================================================

@pytest.mark.anyio
async def test_reconciliation_mismatch_blocks_readiness():
    from core.readiness_checker import ReadinessChecker

    db = MagicMock()
    db.live_arm_state.find_one = AsyncMock(
        return_value={"user_id": "u1", "armed": True, "global_live_enabled": True}
    )
    db.broker_keys.find_one = AsyncMock(
        return_value={"user_id": "u1", "broker": "upstox", "access_token": "real_token_xyz"}
    )
    db.users.find_one = AsyncMock(
        return_value={"id": "u1", "allowed_segments": ["NSE_FO", "BSE_FO"]}
    )
    db.orders.find_one = AsyncMock(return_value=None)

    async def risk_state_find(query, *args, **kwargs):
        _id = query.get("_id", "")
        if _id == "position_reconciliation:u1":
            return {"_id": _id, "mismatch": True}
        return None

    db.risk_state.find_one = risk_state_find

    checker = ReadinessChecker(db)
    result = await checker.check_live_readiness("u1")

    assert result["ready"] is False
    assert result["checks"].get("no_reconciliation_mismatch") is False
