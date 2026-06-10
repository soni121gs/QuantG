"""test_regime_flip_positions.py — regime flip triggers position tightening.

Tests:
  1. CRASH flip: profitable LONG → breakeven lock (sl_price = entry)
  2. CRASH flip: non-profitable LONG → deadline halved
  3. MELTUP flip: query excludes LONG positions (SHORT only affected); no updates fired
"""
import asyncio
import sys
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _make_open_pos(
    pos_id: str,
    side: str,
    entry: float,
    qty: int,
    risk_abs: float,
    ltp: Optional[float] = None,
    deadline_minutes: int = 30,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "id": pos_id,
        "user_id": "u1",
        "position_side": side,
        "average_buy_price": entry,
        "entry_price": entry,
        "open_quantity": qty,
        "r_initial_risk_amount": risk_abs,
        "last_ltp": ltp if ltp is not None else entry,
        "status": "OPEN",
        "deadline_at": (now + timedelta(minutes=deadline_minutes)).isoformat(),
        "tp_sl_tsl_config": {"stoploss_price": entry * 0.9},
    }


def _mock_db(positions: List[Dict]) -> MagicMock:
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=positions)
    db = MagicMock()
    db.strategy_positions.find = MagicMock(return_value=cursor)
    db.strategy_positions.update_one = AsyncMock()
    return db


def test_crash_flip_locks_breakeven_when_profitable():
    """CRASH flip on a profitable LONG position (pnl >= 0.5R) locks SL at entry."""
    from strategy_runner import _tighten_positions_on_regime_flip

    # entry=100, ltp=112, qty=50 → pnl=(112-100)*50=600; risk_abs=500; 0.5R=250; 600>=250 → lock
    pos = _make_open_pos("p1", "LONG", entry=100.0, qty=50, risk_abs=500.0, ltp=112.0)
    db = _mock_db([pos])

    asyncio.run(
        _tighten_positions_on_regime_flip(db, {"regime": "CRASH"}, ["u1"])
    )

    db.strategy_positions.update_one.assert_called_once()
    call_filter, call_update = db.strategy_positions.update_one.call_args[0]
    assert call_filter == {"id": "p1"}
    update_set = call_update["$set"]
    assert update_set.get("sl_price") == 100.0, f"Expected sl_price=100.0, got {update_set.get('sl_price')}"
    assert update_set.get("tp_sl_tsl_config.stoploss_price") == 100.0
    print("PASS test_crash_flip_locks_breakeven_when_profitable")


def test_crash_flip_halves_deadline_when_not_profitable():
    """CRASH flip on a LONG at breakeven (pnl < 0.5R) halves the remaining deadline."""
    from strategy_runner import _tighten_positions_on_regime_flip

    # entry=100, ltp=100, qty=50 → pnl=0; risk_abs=500; 0.5R=250; 0<250 → halve deadline
    pos = _make_open_pos("p2", "LONG", entry=100.0, qty=50, risk_abs=500.0, ltp=100.0,
                         deadline_minutes=30)
    db = _mock_db([pos])

    asyncio.run(
        _tighten_positions_on_regime_flip(db, {"regime": "CRASH"}, ["u1"])
    )

    db.strategy_positions.update_one.assert_called_once()
    _, call_update = db.strategy_positions.update_one.call_args[0]
    update_set = call_update["$set"]
    assert "deadline_at" in update_set, "Expected deadline_at to be halved"
    assert "sl_price" not in update_set, "sl_price must not be set when pnl < 0.5R"
    print("PASS test_crash_flip_halves_deadline_when_not_profitable")


def test_meltup_flip_does_not_affect_long_positions():
    """MELTUP flip only queries SHORT positions; LONG positions are untouched."""
    from strategy_runner import _tighten_positions_on_regime_flip

    # MELTUP: against_side = SHORT; DB is queried for SHORT only.
    # We return an empty list (no SHORT open), so update_one is never called.
    db = _mock_db([])

    asyncio.run(
        _tighten_positions_on_regime_flip(db, {"regime": "MELTUP"}, ["u1"])
    )

    # Confirm the DB was queried with position_side=SHORT
    find_query = db.strategy_positions.find.call_args[0][0]
    assert find_query.get("position_side") == "SHORT", (
        f"MELTUP must query SHORT positions, got {find_query.get('position_side')}"
    )
    db.strategy_positions.update_one.assert_not_called()
    print("PASS test_meltup_flip_does_not_affect_long_positions")


TESTS = [
    test_crash_flip_locks_breakeven_when_profitable,
    test_crash_flip_halves_deadline_when_not_profitable,
    test_meltup_flip_does_not_affect_long_positions,
]

if __name__ == "__main__":
    passed = 0
    failed = 0
    for test_fn in TESTS:
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            print(f"FAIL {test_fn.__name__}: {exc}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
