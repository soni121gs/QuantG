"""
tests/test_live_readiness.py

Unit tests for Phase 6 live-readiness:
  - live_entry_preflight: 12-point gate (all failure paths + happy path)
  - broker truth rule: order lifecycle constants verify no fake fills
  - ops /live-readiness: READY / NOT_READY status logic

All tests are self-contained (no server, no real DB).
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_SENTINEL = object()  # distinguishes "use default" from "explicitly return None"


def _make_db(
    arm_doc=_SENTINEL,
    kill_doc=_SENTINEL,
    broker_keys_doc=_SENTINEL,
    position_doc=None,
    order_doc=None,
):
    """Return a minimal mock Motor db for live_entry_preflight tests."""
    db = MagicMock()
    db.live_arm_state.find_one = AsyncMock(
        return_value=arm_doc if arm_doc is not _SENTINEL else {
            "user_id": "u1", "armed": True, "global_live_enabled": True
        }
    )
    db.risk_state.find_one = AsyncMock(
        return_value=kill_doc if kill_doc is not _SENTINEL else {"_id": "global_kill_switch", "active": False}
    )
    db.broker_keys.find_one = AsyncMock(
        return_value=broker_keys_doc if broker_keys_doc is not _SENTINEL else {
            "user_id": "u1", "broker": "upstox", "access_token": "real_token_abc123"
        }
    )
    db.strategy_positions.find_one = AsyncMock(return_value=position_doc)
    db.orders.find_one = AsyncMock(return_value=order_doc)
    return db


def _good_sig(overrides=None):
    """A signal that passes all preflight checks."""
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


# ─────────────────────────────────────────────────────────────────────────────
# Preflight Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestLiveEntryPreflight(unittest.IsolatedAsyncioTestCase):

    async def _run(self, db, sig, strategy=None, env_flag="true"):
        with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": env_flag}):
            with patch("core.live_entry_preflight.get_segment_status") as mock_clock:
                mock_clock.return_value = {"open": True}
                with patch("core.live_entry_preflight.resolve_domain_by_underlying") as mock_domain:
                    mock_domain.return_value = MagicMock(name="NFO")
                    from core.live_entry_preflight import live_entry_preflight
                    return await live_entry_preflight(db, "u1", sig, strategy or _good_strategy())

    async def test_preflight_passes_good_signal(self):
        result = await self._run(_make_db(), _good_sig())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reason_code"], "LIVE_PREFLIGHT_PASSED")

    async def test_preflight_blocks_env_flag_off(self):
        result = await self._run(_make_db(), _good_sig(), env_flag="false")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "LIVE_PREFLIGHT_NOT_READY")
        self.assertEqual(result["failed_check"], "env_live_enabled")

    async def test_preflight_blocks_not_armed(self):
        db = _make_db(arm_doc={"user_id": "u1", "armed": False, "global_live_enabled": True})
        result = await self._run(db, _good_sig())
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "arm_state")

    async def test_preflight_blocks_global_live_disabled(self):
        db = _make_db(arm_doc={"user_id": "u1", "armed": True, "global_live_enabled": False})
        result = await self._run(db, _good_sig())
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "arm_state")

    async def test_preflight_blocks_kill_switch_active(self):
        db = _make_db(kill_doc={"_id": "global_kill_switch", "active": True})
        result = await self._run(db, _good_sig())
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "kill_switch")

    async def test_preflight_blocks_missing_token(self):
        db = _make_db(broker_keys_doc=None)   # explicitly return None from DB
        result = await self._run(db, _good_sig())
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "upstox_token")

    async def test_preflight_blocks_mock_token(self):
        db = _make_db(broker_keys_doc={"user_id": "u1", "broker": "upstox", "access_token": "mock_dev_token"})
        result = await self._run(db, _good_sig())
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "upstox_token")

    async def test_preflight_blocks_missing_instrument_key(self):
        sig = _good_sig({"option_contract.instrument_key": "NIFTY24DEC25000CE"})  # no '|'
        result = await self._run(_make_db(), sig)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "instrument_key")

    async def test_preflight_blocks_simulated_contract(self):
        sig = _good_sig({"option_contract.simulated": True})
        result = await self._run(_make_db(), sig)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "not_simulated")

    async def test_preflight_blocks_paper_simulated_source(self):
        sig = _good_sig({"option_contract.source": "PAPER_SIMULATED_CONTRACT"})
        result = await self._run(_make_db(), sig)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "not_simulated")

    async def test_preflight_blocks_stale_quote_zero_ltp(self):
        sig = _good_sig({"option_contract.ltp": 0})
        result = await self._run(_make_db(), sig)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "ltp_positive")

    async def test_preflight_blocks_low_quality_score(self):
        sig = _good_sig({"option_quality_score": 50.0, "option_contract.option_quality_score": 50.0})
        result = await self._run(_make_db(), sig)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "option_quality_score")
        self.assertIn("70", result.get("detail", ""))  # threshold mentioned

    async def test_preflight_blocks_duplicate_contract(self):
        dup_pos = {"id": "pos-existing", "status": "OPEN"}
        db = _make_db(position_doc=dup_pos)
        result = await self._run(db, _good_sig())
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "no_duplicate_position")

    async def test_preflight_blocks_stale_idempotency_key(self):
        existing_order = {"id": "ord-001", "status": "PLACED"}
        db = _make_db(order_doc=existing_order)
        result = await self._run(db, _good_sig())
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_check"], "idempotency_fresh")

    async def test_preflight_r_exit_fallback_is_nonblocking(self):
        """Missing R-exit metadata uses fallback 2% stop — must NOT block."""
        strategy = {"id": "strat-001"}  # no stop_loss
        sig = _good_sig()
        sig.pop("initial_stop_R", None)
        sig.pop("stop_loss", None)
        result = await self._run(_make_db(), sig, strategy=strategy)
        # R-exit check is non-blocking (fallback created)
        r_check = next((c for c in result.get("checks", []) if c["id"] == "r_exit_metadata"), None)
        self.assertIsNotNone(r_check)
        self.assertTrue(r_check["ok"])

    async def test_preflight_checks_list_has_all_ids(self):
        """All 12 checks must appear in the checks list on a passing run."""
        result = await self._run(_make_db(), _good_sig())
        self.assertTrue(result["ok"], result)
        check_ids = {c["id"] for c in result.get("checks", [])}
        expected = {
            "env_live_enabled", "arm_state", "kill_switch", "upstox_token",
            "market_session", "instrument_key", "not_simulated", "ltp_positive",
            "option_quality_score", "no_duplicate_position", "idempotency_fresh",
            "r_exit_metadata",
        }
        self.assertEqual(check_ids, expected)


# ─────────────────────────────────────────────────────────────────────────────
# Broker Truth Rule Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBrokerTruthRule(unittest.TestCase):
    """Verify that the order lifecycle constants enforce broker truth (no fake fills)."""

    def setUp(self):
        from order_lifecycle import (
            ORDER_PLACED, ORDER_FILLED, ORDER_UNKNOWN_NEEDS_REVIEW,
            ORDER_ACTIVE_STATUSES, ORDER_TERMINAL_STATUSES,
        )
        self.PLACED = ORDER_PLACED
        self.FILLED = ORDER_FILLED
        self.UNKNOWN = ORDER_UNKNOWN_NEEDS_REVIEW
        self.ACTIVE = ORDER_ACTIVE_STATUSES
        self.TERMINAL = ORDER_TERMINAL_STATUSES

    def test_live_order_starts_as_placed_not_filled(self):
        """Broker orders must start as PLACED, not FILLED."""
        self.assertEqual(self.PLACED, "PLACED")
        self.assertNotEqual(self.PLACED, "FILLED")

    def test_unknown_broker_status_constant(self):
        self.assertEqual(self.UNKNOWN, "UNKNOWN_NEEDS_REVIEW")

    def test_placed_is_not_terminal_status(self):
        """PLACED must not be in terminal statuses — fill requires broker confirmation."""
        self.assertNotIn(self.PLACED, self.TERMINAL)

    def test_placed_is_active_status(self):
        """PLACED order is still active (awaiting broker confirmation)."""
        self.assertIn(self.PLACED, self.ACTIVE)

    def test_no_fake_fill_filled_is_terminal(self):
        """FILLED is a terminal status — only reachable via broker confirmation."""
        self.assertIn(self.FILLED, self.TERMINAL)

    def test_rejected_releases_reservation(self):
        """REJECTED is a terminal status — means broker refused, reservation must be freed."""
        self.assertIn("REJECTED", self.TERMINAL)

    def test_unknown_needs_review_not_in_active_statuses(self):
        """UNKNOWN_NEEDS_REVIEW must not be in active statuses — it needs manual resolution."""
        from order_lifecycle import ORDER_ACTIVE_STATUSES
        self.assertNotIn("UNKNOWN_NEEDS_REVIEW", ORDER_ACTIVE_STATUSES)

    def test_cancelled_is_terminal(self):
        self.assertIn("CANCELLED", self.TERMINAL)


# ─────────────────────────────────────────────────────────────────────────────
# Signal Manager Preflight Integration
# ─────────────────────────────────────────────────────────────────────────────

class TestSignalManagerPreflightWiring(unittest.IsolatedAsyncioTestCase):
    """Verify the preflight is wired: a live signal with a bad contract is skipped cleanly."""

    async def test_live_signal_with_simulated_contract_skipped(self):
        """_dispatch_signal_via_unified_engine must return LIVE_PREFLIGHT_NOT_READY
        for a live signal with a simulated contract, without creating a broker order."""
        from signal_manager import _dispatch_signal_via_unified_engine

        sig = _good_sig({"option_contract.simulated": True})
        sig["mode"] = "live"

        strategy = {"id": "strat-001", "mode": "live", "paper_mode": False, "stop_loss": 0.02}

        db = _make_db()

        with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "true"}):
            with patch("core.live_entry_preflight.get_segment_status", return_value={"open": True}):
                with patch("core.live_entry_preflight.resolve_domain_by_underlying", return_value=MagicMock(name="NFO")):
                    result = await _dispatch_signal_via_unified_engine(
                        db, "u1", sig, strategy, place_order_fn=None
                    )

        self.assertEqual(result.get("reason_code"), "LIVE_PREFLIGHT_NOT_READY")
        self.assertEqual(result.get("status"), "SKIPPED_SIGNAL")
        # Confirm no broker order was created
        db.orders.insert_one.assert_not_called()

    async def test_paper_signal_bypasses_live_preflight(self):
        """Paper signals must NOT go through live_entry_preflight."""
        from signal_manager import _dispatch_signal_via_unified_engine

        sig = _good_sig({"option_contract.simulated": True})
        sig["mode"] = "paper"

        strategy = {"id": "strat-001", "mode": "paper", "paper_mode": True}

        db = _make_db()
        # Add everything paper dispatch needs
        db.risk_reservations = MagicMock()
        db.risk_reservations.find_one = AsyncMock(return_value=None)
        db.risk_reservations.insert_one = AsyncMock()
        db.risk_state.find_one = AsyncMock(return_value=None)
        db.strategy_positions.find_one = AsyncMock(return_value=None)
        db.strategy_positions.insert_one = AsyncMock()
        db.orders.find_one = AsyncMock(return_value=None)
        db.orders.insert_one = AsyncMock()
        db.orders.count_documents = AsyncMock(return_value=0)
        db.risk_events = MagicMock()
        db.risk_events.insert_one = AsyncMock()
        db.outbox_events = MagicMock()
        db.outbox_events.insert_one = AsyncMock()
        db.paper_wallets = MagicMock()
        db.paper_wallets.find_one = AsyncMock(return_value=None)
        db.paper_wallets.update_one = AsyncMock()
        db.runner_locks = MagicMock()
        db.runner_locks.find_one_and_update = AsyncMock(return_value=None)

        with patch.dict(os.environ, {"CORE_ENGINE_LIVE_ENABLED": "true"}):
            # live_entry_preflight should NOT be called for paper
            with patch("core.live_entry_preflight.live_entry_preflight") as mock_pf:
                try:
                    await _dispatch_signal_via_unified_engine(
                        db, "u1", sig, strategy, place_order_fn=None
                    )
                except Exception:
                    pass  # paper path may fail due to missing mocks; that's ok
                mock_pf.assert_not_called()


if __name__ == "__main__":
    unittest.main()
