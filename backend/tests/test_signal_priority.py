"""
Tests for Phase 5 — Signal Priority and Exposure Control.

Covers:
  T1  - Exits are always approved before entries
  T2  - Active NIFTY position blocks new NIFTY entry (SKIPPED_GROUP_EXPOSURE_ACTIVE)
  T3  - Best confidence wins when same group fires
  T4  - option_quality_score improves ranking (same confidence, different quality)
  T5  - target_R improves ranking (same confidence + quality, different R)
  T6  - Lower-priority signal becomes SKIPPED_SIGNAL, not FAILED
  T7  - One NIFTY and one BANKNIFTY can both pass in same batch
  T8  - Tie-breaker is deterministic (strategy_id lexicographic fallback)
  T9  - compute_priority_score formula is correct
"""
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from signal_manager import ConflictResolver, compute_priority_score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sig(
    sig_id: str,
    strategy_id: str,
    symbol: str = "NIFTY",
    action: str = "BUY",
    confidence: float = 85.0,
    option_quality_score: float | None = None,
    target_R: float = 2.0,
    warnings: list | None = None,
    created_at: str | None = None,
) -> dict:
    s: dict = {
        "id": sig_id,
        "user_id": "user-1",
        "strategy_id": strategy_id,
        "symbol": symbol,
        "target_symbol": f"{symbol}24JUN25000CE",
        "action": action,
        "confidence": confidence,
        "target_R": target_R,
        "status": "PENDING",
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "created_at": created_at or _now_iso(),
    }
    if option_quality_score is not None:
        s["option_quality_score"] = option_quality_score
    if warnings:
        s["v2_selector_warnings"] = warnings
    return s


def _active_pos(strategy_id: str, symbol: str = "NIFTY", status: str = "OPEN") -> dict:
    return {
        "id": f"pos-{strategy_id}",
        "strategy_id": strategy_id,
        "symbol": symbol,
        "symbol_group": symbol,
        "status": status,
    }


# ---------------------------------------------------------------------------
# T1 – Exits always approved before entries
# ---------------------------------------------------------------------------

def test_exits_approved_before_entries():
    """
    Exit signal for a strategy that has an open position must always be approved.
    A *different* strategy that is NOT exiting holds the group lock, so a new entry
    for that group must be blocked (SKIPPED_GROUP_EXPOSURE_ACTIVE).
    """
    # strat-exiting has an open position and sends a SELL (exit)
    exit_sig = _sig("exit-1", "strat-exiting", symbol="BANKNIFTY", action="SELL", confidence=50.0)
    # strat-holder also has an open position in BANKNIFTY but is NOT sending an exit
    # strat-new wants to enter BANKNIFTY
    entry_sig = _sig("entry-1", "strat-new", symbol="BANKNIFTY", action="BUY", confidence=99.0)

    pos_exiting = _active_pos("strat-exiting", "BANKNIFTY")
    pos_holder = _active_pos("strat-holder", "BANKNIFTY")

    approved, rejected = ConflictResolver.resolve(
        pending_signals=[entry_sig, exit_sig],
        active_positions=[pos_exiting, pos_holder],
        one_active_position_per_symbol_group=True,
    )

    approved_ids = {s["id"] for s in approved}
    rejected_ids = {s["id"] for s in rejected}

    assert "exit-1" in approved_ids, "Exit must always be approved"
    assert "entry-1" in rejected_ids, "New entry blocked because strat-holder holds the group lock"
    assert rejected[0]["rejection_reason"] == "SKIPPED_GROUP_EXPOSURE_ACTIVE"


# ---------------------------------------------------------------------------
# T2 – Active NIFTY position blocks new NIFTY entry
# ---------------------------------------------------------------------------

def test_active_nifty_position_blocks_new_entry():
    pos = _active_pos("strat-existing", "NIFTY")
    entry = _sig("entry-blocked", "strat-new", symbol="NIFTY", confidence=95.0)

    approved, rejected = ConflictResolver.resolve(
        pending_signals=[entry],
        active_positions=[pos],
        one_active_position_per_symbol_group=True,
    )

    assert approved == []
    assert len(rejected) == 1
    assert rejected[0]["rejection_reason"] == "SKIPPED_GROUP_EXPOSURE_ACTIVE"
    assert rejected[0]["id"] == "entry-blocked"


# ---------------------------------------------------------------------------
# T3 – Best confidence wins when two entries fire for same group
# ---------------------------------------------------------------------------

def test_best_confidence_wins_same_group():
    low = _sig("low-conf", "strat-a", confidence=75.0)
    high = _sig("high-conf", "strat-b", confidence=92.0)

    approved, rejected = ConflictResolver.resolve(
        pending_signals=[low, high],
        active_positions=[],
        one_active_position_per_symbol_group=True,
    )

    approved_ids = {s["id"] for s in approved}
    rejected_ids = {s["id"] for s in rejected}

    assert "high-conf" in approved_ids
    assert "low-conf" in rejected_ids
    assert rejected[0]["rejection_reason"] == "SKIPPED_LOWER_PRIORITY_SIGNAL"
    assert rejected[0]["selected_signal_id"] == "high-conf"


# ---------------------------------------------------------------------------
# T4 – option_quality_score improves ranking
# ---------------------------------------------------------------------------

def test_option_quality_score_improves_ranking():
    """Same confidence; higher option_quality_score should win."""
    low_q = _sig("low-q", "strat-a", confidence=85.0, option_quality_score=50.0)
    high_q = _sig("high-q", "strat-b", confidence=85.0, option_quality_score=90.0)

    # priority_score(low_q)  = 85 + 50*0.4 + 2*5 = 85+20+10 = 115
    # priority_score(high_q) = 85 + 90*0.4 + 2*5 = 85+36+10 = 131
    approved, rejected = ConflictResolver.resolve(
        pending_signals=[low_q, high_q],
        active_positions=[],
        one_active_position_per_symbol_group=True,
    )

    approved_ids = {s["id"] for s in approved}
    rejected_ids = {s["id"] for s in rejected}

    assert "high-q" in approved_ids
    assert "low-q" in rejected_ids
    assert rejected[0]["selected_signal_id"] == "high-q"


# ---------------------------------------------------------------------------
# T5 – target_R improves ranking
# ---------------------------------------------------------------------------

def test_target_R_improves_ranking():
    """Same confidence + quality; higher target_R should win."""
    low_r = _sig("low-r", "strat-a", confidence=85.0, option_quality_score=70.0, target_R=1.5)
    high_r = _sig("high-r", "strat-b", confidence=85.0, option_quality_score=70.0, target_R=3.0)

    # priority_score(low_r)  = 85 + 28 + 7.5 = 120.5
    # priority_score(high_r) = 85 + 28 + 15  = 128
    approved, rejected = ConflictResolver.resolve(
        pending_signals=[low_r, high_r],
        active_positions=[],
        one_active_position_per_symbol_group=True,
    )

    approved_ids = {s["id"] for s in approved}
    assert "high-r" in approved_ids
    assert "low-r" not in approved_ids
    assert rejected[0]["selected_signal_id"] == "high-r"


# ---------------------------------------------------------------------------
# T6 – Lower-priority signal gets status=SKIPPED_SIGNAL (not FAILED)
# ---------------------------------------------------------------------------

def test_lower_priority_signal_status_is_skipped_signal():
    """Rejected signals from priority resolution must carry SKIPPED_SIGNAL status,
    not a FAILED broker status."""
    winner = _sig("winner", "strat-a", confidence=92.0)
    loser = _sig("loser", "strat-b", confidence=80.0)

    _, rejected = ConflictResolver.resolve(
        pending_signals=[winner, loser],
        active_positions=[],
        one_active_position_per_symbol_group=True,
    )

    assert len(rejected) == 1
    r = rejected[0]
    assert r["id"] == "loser"
    assert r["rejection_reason"] == "SKIPPED_LOWER_PRIORITY_SIGNAL"
    # Signal should not carry any broker-level failure indicator
    assert r.get("status") != "FAILED"
    assert r.get("status") != "REJECTED"


# ---------------------------------------------------------------------------
# T7 – One NIFTY and one BANKNIFTY can both pass in same batch
# ---------------------------------------------------------------------------

def test_nifty_and_banknifty_both_approved():
    """NIFTY and BANKNIFTY are distinct symbol groups; both entries should pass."""
    nifty_sig = _sig("nifty-1", "strat-n", symbol="NIFTY", confidence=88.0)
    # Use explicit symbol_group to avoid any substring ambiguity
    bnf_sig = {
        "id": "bnf-1",
        "user_id": "user-1",
        "strategy_id": "strat-b",
        "symbol": "BANKNIFTY",
        "symbol_group": "BANKNIFTY",
        "target_symbol": "BANKNIFTY24JUN55000CE",
        "action": "BUY",
        "confidence": 85.0,
        "target_R": 2.0,
        "status": "PENDING",
        "visual_config": {"options": {"strike_mode": "ATM_BUY"}},
        "created_at": _now_iso(),
    }

    approved, rejected = ConflictResolver.resolve(
        pending_signals=[nifty_sig, bnf_sig],
        active_positions=[],
        one_active_position_per_symbol_group=True,
    )

    approved_ids = {s["id"] for s in approved}
    assert "nifty-1" in approved_ids, "NIFTY entry should pass"
    assert "bnf-1" in approved_ids, "BANKNIFTY entry should pass independently"
    assert rejected == []


# ---------------------------------------------------------------------------
# T8 – Tie-breaker is deterministic when all scores match
# ---------------------------------------------------------------------------

def test_tie_breaker_is_deterministic():
    """When priority_score, confidence, quality_score, and created_at are all equal,
    the larger strategy_id string must always win."""
    ts = _now_iso()
    sig_a = _sig("sig-a", "strat-aaa", confidence=85.0, option_quality_score=70.0, created_at=ts)
    sig_b = _sig("sig-b", "strat-zzz", confidence=85.0, option_quality_score=70.0, created_at=ts)

    # Run twice to verify result is stable regardless of input order
    approved1, rejected1 = ConflictResolver.resolve(
        pending_signals=[sig_a, sig_b],
        active_positions=[],
        one_active_position_per_symbol_group=True,
    )
    approved2, rejected2 = ConflictResolver.resolve(
        pending_signals=[sig_b, sig_a],
        active_positions=[],
        one_active_position_per_symbol_group=True,
    )

    winner_ids1 = {s["id"] for s in approved1}
    winner_ids2 = {s["id"] for s in approved2}

    # strat-zzz > strat-aaa lexicographically, so sig-b should win both times
    assert "sig-b" in winner_ids1
    assert "sig-b" in winner_ids2
    assert "sig-a" not in winner_ids1
    assert "sig-a" not in winner_ids2


# ---------------------------------------------------------------------------
# T9 – compute_priority_score formula is correct
# ---------------------------------------------------------------------------

def test_compute_priority_score_formula():
    # confidence=90, quality=80, target_R=3, warnings=1 (penalty=5)
    # = 90 + 80*0.4 + 3*5 - 5
    # = 90 + 32 + 15 - 5 = 132
    sig = {
        "confidence": 90.0,
        "option_quality_score": 80.0,
        "target_R": 3.0,
        "v2_selector_warnings": ["WIDE_SPREAD"],
    }
    assert compute_priority_score(sig) == 132.0

    # No optional fields — safe defaults: confidence=85, quality=0, target_R=2, penalty=0
    # = 85 + 0 + 10 - 0 = 95
    sig_minimal = {}
    assert compute_priority_score(sig_minimal) == 95.0

    # Penalty capped at 20 (4+ warnings)
    sig_many_warns = {
        "confidence": 85.0,
        "v2_selector_warnings": ["W1", "W2", "W3", "W4", "W5"],
    }
    # = 85 + 0 + 10 - 20 = 75
    assert compute_priority_score(sig_many_warns) == 75.0
