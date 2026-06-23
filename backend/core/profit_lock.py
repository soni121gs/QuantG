"""Day-level profit lock — capture open profit instead of riding it back down.

Motivation (2026-06-23): on a strong trend day the book's aggregate unrealized
P&L peaked around +25k, but nothing booked it — every exit decision in QuantG is
per-position (position_lifecycle), so there was no mechanism watching the day's
realized+unrealized total. The two big BANKNIFTY winners rode all the way up and
gave a chunk back before the 15:10 square-off. This module adds the missing
"lock the day's gains" layer.

Two layers, one evaluator (single writer of lock state):
  • Per-strategy: arm once a strategy's day P&L (realized today_pnl + open
    unrealized) crosses PROFIT_LOCK_STRAT_ARM, then square that strategy off and
    stand it down for the day if it gives back PROFIT_LOCK_STRAT_GIVEBACK from its
    own peak. Catches each winner near its top.
  • Whole-book: arm once the user's aggregate day P&L crosses
    PROFIT_LOCK_BOOK_ARM, trail by PROFIT_LOCK_BOOK_GIVEBACK, plus a hard ceiling
    PROFIT_LOCK_BOOK_CEILING that books the entire book immediately. The safety
    net for the aggregate peak.

State lives in db.profit_lock_state, keyed by IST date, so it resets each day.
A locked strategy carries day_profit_locked=True + day_profit_locked_date on its
strategies doc; the signal_manager entry gate reads that and blocks re-entry for
the rest of the day. This module is the ONLY writer of those lock fields.

All thresholds are env-overridable. Disable entirely with PROFIT_LOCK_ENABLED=false.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Coroutine, Dict, Optional, Tuple

logger = logging.getLogger("quantg.profit_lock")

ENABLED = os.environ.get("PROFIT_LOCK_ENABLED", "true").lower() == "true"

# Per-strategy layer
STRAT_ARM = float(os.environ.get("PROFIT_LOCK_STRAT_ARM", "4000"))
STRAT_GIVEBACK = float(os.environ.get("PROFIT_LOCK_STRAT_GIVEBACK", "0.30"))

# Whole-book layer
BOOK_ARM = float(os.environ.get("PROFIT_LOCK_BOOK_ARM", "15000"))
BOOK_GIVEBACK = float(os.environ.get("PROFIT_LOCK_BOOK_GIVEBACK", "0.30"))
BOOK_CEILING = float(os.environ.get("PROFIT_LOCK_BOOK_CEILING", "30000"))

_OPEN_STATUSES = ["PENDING_BROKER", "OPEN", "FILLED"]


def _ist_date() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def decide_lock(
    current: float,
    peak: float,
    armed: bool,
    *,
    arm: float,
    giveback: float,
    ceiling: Optional[float] = None,
) -> Tuple[bool, float, bool, Optional[str]]:
    """Pure decision. Returns (lock, new_peak, new_armed, reason).

    - new_peak ratchets up and never falls.
    - armed latches True once current >= arm.
    - once armed: lock if a hard ceiling is crossed, or if current has retraced
      `giveback` fraction from the peak.
    """
    new_peak = peak if peak > current else current
    new_armed = armed or current >= arm
    if new_armed:
        if ceiling is not None and current >= ceiling:
            return True, new_peak, new_armed, "ceiling"
        if new_peak > 0 and current <= new_peak * (1.0 - giveback):
            return True, new_peak, new_armed, "trail"
    return False, new_peak, new_armed, None


async def _get_state(db, date: str, scope: str, key: str) -> Dict:
    doc = await db.profit_lock_state.find_one({"_id": f"{date}:{scope}:{key}"})
    return doc or {"peak": 0.0, "armed": False, "locked": False}


async def _save_state(db, date: str, scope: str, key: str, peak: float, armed: bool, locked: bool) -> None:
    await db.profit_lock_state.update_one(
        {"_id": f"{date}:{scope}:{key}"},
        {"$set": {
            "date": date, "scope": scope, "key": key,
            "peak": round(peak, 2), "armed": bool(armed), "locked": bool(locked),
            "updated_at": _now(),
        }},
        upsert=True,
    )


async def _square_off_and_stand_down(
    db, close_fn: Callable[..., Coroutine], uid: str, sid: str,
    name: Optional[str], day_pnl: float, peak: float, reason: str, scope: str,
) -> None:
    """Close all of a strategy's open positions and flag it locked for the day."""
    try:
        await close_fn(uid, sid, reason=f"profit-lock-{scope}-{reason}")
    except Exception as exc:
        logger.error("profit_lock: close_fn failed strat=%s: %s", sid, exc)
    await db.strategies.update_one(
        {"id": sid, "user_id": uid},
        {"$set": {
            "day_profit_locked": True,
            "day_profit_locked_date": _ist_date(),
            "day_profit_locked_reason": f"{scope}-{reason}",
            "updated_at": _now(),
        }},
    )
    logger.warning(
        "PROFIT LOCK [%s/%s] strat=%s name=%s day_pnl=%.0f peak=%.0f → squared off + standing down",
        scope, reason, sid, name, day_pnl, peak,
    )


async def evaluate_and_apply_locks(db, close_fn: Callable[..., Coroutine]) -> None:
    """Run one evaluation pass across all users' strategies. Call from the monitor
    tick (in-hours only). Idempotent: a strategy already flagged locked is skipped."""
    if not ENABLED:
        return
    date = _ist_date()

    # Sum each strategy's open unrealized P&L, and track which strategies are open.
    open_rows = await db.strategy_positions.find(
        {"status": {"$in": _OPEN_STATUSES}},
        {"_id": 0, "strategy_id": 1, "user_id": 1, "unrealized_pnl": 1},
    ).to_list(5000)
    unreal: Dict[Tuple[str, str], float] = {}
    open_sids: Dict[str, set] = {}
    for r in open_rows:
        sid = r.get("strategy_id")
        uid = r.get("user_id")
        if not sid or not uid:
            continue
        unreal[(uid, sid)] = unreal.get((uid, sid), 0.0) + float(r.get("unrealized_pnl") or 0.0)
        open_sids.setdefault(uid, set()).add(sid)

    strats = await db.strategies.find(
        {}, {"_id": 0, "id": 1, "user_id": 1, "today_pnl": 1, "name": 1, "status": 1},
    ).to_list(5000)

    by_user: Dict[str, list] = {}
    for s in strats:
        uid = s.get("user_id")
        if uid:
            by_user.setdefault(uid, []).append(s)

    for uid, slist in by_user.items():
        book_pnl = 0.0
        # ── Per-strategy layer ──────────────────────────────────────────────
        for s in slist:
            sid = s.get("id")
            day_pnl = float(s.get("today_pnl") or 0.0) + unreal.get((uid, sid), 0.0)
            book_pnl += day_pnl
            st = await _get_state(db, date, "strat", sid)
            if st.get("locked"):
                continue
            lock, peak, armed, reason = decide_lock(
                day_pnl, float(st.get("peak") or 0.0), bool(st.get("armed")),
                arm=STRAT_ARM, giveback=STRAT_GIVEBACK,
            )
            await _save_state(db, date, "strat", sid, peak, armed, lock)
            if lock:
                await _square_off_and_stand_down(
                    db, close_fn, uid, sid, s.get("name"), day_pnl, peak, reason, "strat",
                )

        # ── Whole-book layer (safety net for the aggregate peak) ────────────
        bst = await _get_state(db, date, "book", uid)
        if bst.get("locked"):
            continue
        blk, bpeak, barmed, breason = decide_lock(
            book_pnl, float(bst.get("peak") or 0.0), bool(bst.get("armed")),
            arm=BOOK_ARM, giveback=BOOK_GIVEBACK, ceiling=BOOK_CEILING,
        )
        await _save_state(db, date, "book", uid, bpeak, barmed, blk)
        if blk:
            logger.warning(
                "PROFIT LOCK [book/%s] user=%s book_pnl=%.0f peak=%.0f → squaring off ENTIRE book",
                breason, uid, book_pnl, bpeak,
            )
            # Square off every open strategy, then stand the whole live book down
            # so nothing re-enters for the rest of the day.
            for sid in open_sids.get(uid, set()):
                await _square_off_and_stand_down(
                    db, close_fn, uid, sid, None, book_pnl, bpeak, breason, "book",
                )
            await db.strategies.update_many(
                {"user_id": uid, "status": "live"},
                {"$set": {
                    "day_profit_locked": True,
                    "day_profit_locked_date": date,
                    "day_profit_locked_reason": f"book-{breason}",
                    "updated_at": _now(),
                }},
            )
