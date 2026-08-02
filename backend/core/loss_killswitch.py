"""Day-level loss kill-switch — the hard stop (sibling of core/profit_lock).

profit_lock books the day's GAINS; this enforces the day's LOSS floor. Two layers,
one evaluator, run from the monitor tick so it fires even when no new order is
attempted — the preflight daily-loss guard only triggers on a *new entry*, so a
bleeding book that has stopped signalling would never hit it. This is the active,
always-on enforcement.

  • Per-strategy: when a strategy's day P&L (realized today_pnl + open unrealized)
    falls to -daily_loss_limit, square it off and stand it down for the IST day.
  • Whole-book: when the user's aggregate day P&L falls to
    -PORTFOLIO_DAILY_LOSS_LIMIT, square off the entire book and stand it down.

Single writer of the day_loss_locked fields on strategies. The signal_manager
entry gate reads day_loss_locked and blocks re-entry for the rest of the IST day.
Resets next day (date-stamped). Disable with LOSS_KILLSWITCH_ENABLED=false.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Coroutine, Dict, Tuple

logger = logging.getLogger("quantg.loss_killswitch")

ENABLED = os.environ.get("LOSS_KILLSWITCH_ENABLED", "true").lower() == "true"
# Aggregate (whole-book) floor. Paper default sized above normal daily variance but
# below the kind of −24k bug-day that motivated the campaign. Env-overridable.
PORTFOLIO_DAILY_LOSS_LIMIT = float(os.environ.get("PORTFOLIO_DAILY_LOSS_LIMIT", "20000"))

# ── 2026-08-03: scope the BOOK sweep so one sleeve cannot liquidate another ──
# Measured on the real book (631 closed trades): the book-level sweep fired on 5
# consecutive days and force-closed 29 positions for −₹24,859, of which
# −₹20,811 (84%) were DEBIT spreads closed at a 0% win rate. The seller sleeve's
# bleed was arming a trigger that liquidated the long-premium sleeve — the only
# structure in the book that beat its own breakeven (n=46, +₹7,975).
#
# Two narrow exemptions, both reversible by env:
#   1. A strategy whose OWN day P&L is not negative is not part of the problem;
#      closing it converts someone else's loss into its realized loss.
#   2. A DEFINED-RISK hold-to-expiry position has a max loss that is already
#      bounded by its bought wing and already capital-reserved. Force-closing it
#      on an unrealized-P&L trigger does not reduce risk — the worst case was
#      known at entry — it just realizes the loss at peak fear and forfeits the
#      decay/convexity the structure exists to capture. This is the same
#      exemption position_monitor and the EOD backstop already grant (§22.3);
#      the kill-switch was the remaining path that ignored it.
#
# The floor itself is UNCHANGED and still fires. This only narrows WHICH
# strategies the book sweep closes, never whether it triggers.
SLEEVE_SCOPED = os.environ.get("LOSS_KILLSWITCH_SLEEVE_SCOPED", "true").lower() == "true"
SKIP_HOLD_TO_EXPIRY = os.environ.get("LOSS_KILLSWITCH_SKIP_HOLD_TO_EXPIRY", "true").lower() == "true"

_OPEN_STATUSES = ["PENDING_BROKER", "OPEN", "FILLED"]
_DEFINED_RISK_STRUCTURES = {"credit_spread", "debit_spread", "iron_condor"}


def _is_hold_to_expiry(strategy: Dict) -> bool:
    vc = strategy.get("visual_config") or {}
    opt = vc.get("options") or {}
    risk = vc.get("risk") or {}
    return (str(opt.get("exit_mode") or "").lower() == "expiry"
            or str(risk.get("exit_mode") or "").lower() == "hold_to_expiry")


def _is_defined_risk(strategy: Dict) -> bool:
    opt = (strategy.get("visual_config") or {}).get("options") or {}
    return str(opt.get("structure") or "") in _DEFINED_RISK_STRUCTURES


def _ist_date() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strategy_loss_limit(risk: Dict) -> float:
    """Per-strategy daily loss floor (paper-aware). 0 = disabled for that strategy."""
    try:
        return float(risk.get("daily_loss_limit_paper") or risk.get("daily_loss_limit") or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def _stand_down(db, close_fn: Callable[..., Coroutine], uid: str, sid: str,
                      name, day_pnl: float, limit: float, scope: str) -> None:
    """Close all of a strategy's open positions and flag it loss-locked for the day."""
    try:
        await close_fn(uid, sid, reason=f"daily-loss-killswitch-{scope}")
    except Exception as exc:
        logger.error("loss_killswitch: close_fn failed strat=%s: %s", sid, exc)
    await db.strategies.update_one(
        {"id": sid, "user_id": uid},
        {"$set": {
            "day_loss_locked": True,
            "day_loss_locked_date": _ist_date(),
            "day_loss_locked_reason": f"{scope}:{int(day_pnl)}<=-{int(limit)}",
            "updated_at": _now(),
        }},
    )
    logger.error(
        "LOSS KILL-SWITCH [%s] strat=%s name=%s day_pnl=%.0f limit=%.0f → squared off + standing down",
        scope, sid, name, day_pnl, limit,
    )


async def evaluate_loss_killswitch(db, close_fn: Callable[..., Coroutine]) -> None:
    """One evaluation pass across all users. Call from the monitor tick (in-hours).
    Idempotent: a strategy already day_loss_locked today is skipped."""
    if not ENABLED:
        return
    date = _ist_date()

    # Sum each strategy's open unrealized P&L.
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
        {}, {"_id": 0, "id": 1, "user_id": 1, "today_pnl": 1, "name": 1, "status": 1,
             "visual_config": 1, "day_loss_locked": 1, "day_loss_locked_date": 1},
    ).to_list(5000)

    by_user: Dict[str, list] = {}
    for s in strats:
        uid = s.get("user_id")
        if uid:
            by_user.setdefault(uid, []).append(s)

    for uid, slist in by_user.items():
        book_pnl = 0.0
        day_pnl_by_sid: Dict[str, float] = {}
        strat_by_sid: Dict[str, Dict] = {}
        # ── Per-strategy layer ──────────────────────────────────────────────
        for s in slist:
            sid = s.get("id")
            day_pnl = float(s.get("today_pnl") or 0.0) + unreal.get((uid, sid), 0.0)
            book_pnl += day_pnl
            day_pnl_by_sid[sid] = day_pnl
            strat_by_sid[sid] = s
            if s.get("day_loss_locked") and s.get("day_loss_locked_date") == date:
                continue
            # A defined-risk hold-to-expiry position's worst case is bounded by
            # its bought wing and already reserved, so an unrealized drawdown is
            # not new risk information — see the module header. Its REALIZED
            # today_pnl still counts, so a strategy that has actually banked
            # losses is still stood down.
            if SKIP_HOLD_TO_EXPIRY and _is_hold_to_expiry(s) and _is_defined_risk(s):
                realized_only = float(s.get("today_pnl") or 0.0)
                limit = _strategy_loss_limit((s.get("visual_config") or {}).get("risk") or {})
                if limit > 0 and realized_only <= -abs(limit):
                    await _stand_down(db, close_fn, uid, sid, s.get("name"),
                                      realized_only, limit, "strat")
                continue
            limit = _strategy_loss_limit((s.get("visual_config") or {}).get("risk") or {})
            if limit > 0 and day_pnl <= -abs(limit):
                await _stand_down(db, close_fn, uid, sid, s.get("name"), day_pnl, limit, "strat")

        # ── Whole-book layer ────────────────────────────────────────────────
        if PORTFOLIO_DAILY_LOSS_LIMIT > 0 and book_pnl <= -abs(PORTFOLIO_DAILY_LOSS_LIMIT):
            bkey = f"{date}:book:{uid}"
            bdoc = await db.loss_killswitch_state.find_one({"_id": bkey})
            if bdoc and bdoc.get("locked"):
                continue
            # Decide WHICH strategies this sweep closes. The trigger itself is
            # unchanged; only the blast radius is scoped.
            sweep, spared = [], []
            for sid in open_sids.get(uid, set()):
                s = strat_by_sid.get(sid) or {}
                sid_pnl = day_pnl_by_sid.get(sid, 0.0)
                if SLEEVE_SCOPED and sid_pnl >= 0:
                    spared.append((sid, "not_losing", sid_pnl))
                elif SKIP_HOLD_TO_EXPIRY and _is_hold_to_expiry(s) and _is_defined_risk(s):
                    spared.append((sid, "defined_risk_hold_to_expiry", sid_pnl))
                else:
                    sweep.append(sid)
            logger.error(
                "LOSS KILL-SWITCH [book] user=%s book_pnl=%.0f limit=%.0f → squaring off %d strategies "
                "(sparing %d: %s)",
                uid, book_pnl, PORTFOLIO_DAILY_LOSS_LIMIT, len(sweep), len(spared),
                ", ".join(f"{s}:{r}" for s, r, _ in spared) or "none",
            )
            for sid in sweep:
                await _stand_down(db, close_fn, uid, sid, None, book_pnl, PORTFOLIO_DAILY_LOSS_LIMIT, "book")
            # Lock re-entry for the day. Strategies the sweep deliberately spared
            # must NOT be locked here — locking them would achieve by the back door
            # exactly what sparing them was meant to avoid (an untouched, still-open
            # sleeve that can no longer manage or re-enter its own position).
            _lock_filter: Dict = {"user_id": uid, "status": "live"}
            _spared_ids = [s for s, _, _ in spared]
            if _spared_ids:
                _lock_filter["id"] = {"$nin": _spared_ids}
            await db.strategies.update_many(
                _lock_filter,
                {"$set": {
                    "day_loss_locked": True,
                    "day_loss_locked_date": date,
                    "day_loss_locked_reason": f"book:{int(book_pnl)}<=-{int(PORTFOLIO_DAILY_LOSS_LIMIT)}",
                    "updated_at": _now(),
                }},
            )
            await db.loss_killswitch_state.update_one(
                {"_id": bkey},
                {"$set": {"date": date, "scope": "book", "user_id": uid, "locked": True,
                          "book_pnl": round(book_pnl, 2), "updated_at": _now(),
                          "swept": sweep,
                          "spared": [{"strategy_id": s, "reason": r, "day_pnl": round(p, 2)}
                                     for s, r, p in spared]}},
                upsert=True,
            )
