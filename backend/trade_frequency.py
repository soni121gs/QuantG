"""Trade frequency controls — per-strategy daily caps, loss-streak throttle,
time-of-day volume baseline, and filter rejection logger.

Strategy classes and caps (all config-overridable via env):

  Class          Strategies                       Daily cap  Cooldown bars
  ─────────────────────────────────────────────────────────────────────────
  scalper        ORB, BANKNIFTY HFT, EMA Scalper  12–20      2–3
  momentum       ATM Momentum, BN Breakout         6–10       4–5
  trend_retest   VWAP, Micro-Lot                   4–6        8–10
  swing          SENSEX RSI                        2–4        —

Effective cap = base_cap × freq_multiplier (from MarketRegime).
Loss-streak throttle: 3 consecutive SL hits → 30-min pause, then half-cap.

Filter rejection log: every evaluation logs which gate rejected it.
EOD per-strategy summary: evaluated / rejected-per-filter / fired / cap-limited.
Zero-signal alert: if a strategy fires 0 signals for 3 sessions, logs the
top rejecting filter.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("quantg.trade_frequency")

# ── Strategy class config ──────────────────────────────────────────────────────

_STRATEGY_CLASSES: Dict[str, str] = {
    # Scalpers (ORB = NIFTY HFT Quick Scalper)
    "NIFTY HFT Quick Scalper":          "scalper",
    "BANKNIFTY HFT Momentum Scalper":   "scalper",
    "NIFTY Quick EMA Scalper":          "scalper",
    # Momentum / Breakout
    "UPSTOX NIFTY ATM Option Momentum Buyer":    "momentum",
    "UPSTOX BANKNIFTY ATM Option Breakout Buyer":"momentum",
    "BANKNIFTY Volatility Breakout":             "momentum",
    # Trend / Retest
    "NIFTY VWAP Trend Breakout":        "trend_retest",
    "NIFTY Micro-Lot Trend Follower":   "trend_retest",
    # Swing
    "SENSEX Swing RSI Pullback":        "swing",
}

_CLASS_CAPS: Dict[str, Dict[str, Any]] = {
    "scalper":      {"daily_cap": int(os.environ.get("FREQ_CAP_SCALPER",      "15")), "cooldown_bars": 2},
    "momentum":     {"daily_cap": int(os.environ.get("FREQ_CAP_MOMENTUM",      "8")), "cooldown_bars": 4},
    "trend_retest": {"daily_cap": int(os.environ.get("FREQ_CAP_TREND",         "5")), "cooldown_bars": 8},
    "swing":        {"daily_cap": int(os.environ.get("FREQ_CAP_SWING",         "3")), "cooldown_bars": 0},
    "default":      {"daily_cap": int(os.environ.get("FREQ_CAP_DEFAULT",       "6")), "cooldown_bars": 4},
}

LOSS_STREAK_TRIGGER  = int(os.environ.get("LOSS_STREAK_TRIGGER",  "3"))
LOSS_STREAK_PAUSE_MIN= int(os.environ.get("LOSS_STREAK_PAUSE_MIN","30"))
LOSS_STREAK_HALF_CAP = os.environ.get("LOSS_STREAK_HALF_CAP", "true").lower() == "true"
PROFIT_CAP_BOOST_MAX = int(os.environ.get("FREQ_PROFIT_CAP_BOOST_MAX", "2"))


def _today_window() -> Tuple[str, str]:
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    start = ist.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=5, minutes=30)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _strategy_class(strategy_name: str) -> str:
    return _STRATEGY_CLASSES.get(strategy_name, "default")


async def check_frequency_gate(
    db,
    strategy_id: str,
    strategy_name: str,
    user_id: str,
    freq_multiplier: float = 1.0,
) -> Tuple[bool, Optional[str]]:
    """Return (ok, reason). Checks daily cap and loss-streak throttle."""
    start, end = _today_window()
    cls = _strategy_class(strategy_name)
    cfg = _CLASS_CAPS.get(cls) or _CLASS_CAPS["default"]
    base_cap = cfg["daily_cap"]

    # Effective cap with regime freq_multiplier
    effective_cap = max(1, int(base_cap * max(0.1, freq_multiplier)))

    profit_boost = 0
    try:
        strategy = await db.strategies.find_one(
            {"id": strategy_id, "user_id": user_id},
            {"_id": 0, "today_pnl": 1},
        )
        today_pnl = float((strategy or {}).get("today_pnl") or 0)
        if today_pnl > 0:
            profit_boost = min(PROFIT_CAP_BOOST_MAX, 1 + int(today_pnl >= 1000))
            effective_cap += profit_boost
    except Exception:
        pass

    # Count today's true ENTRY orders for this strategy. Option-buying exits are
    # SELL orders with exit idempotency keys, so counting both sides prematurely
    # shuts profitable strategies down after only a couple of round trips.
    try:
        today_entries = await db.orders.count_documents({
            "strategy_id": strategy_id,
            "user_id": user_id,
            "created_at": {"$gte": start, "$lt": end},
            "status": {"$nin": ["REJECTED", "CANCELLED", "FAILED", "SKIPPED", "SKIPPED_SIGNAL", "STALE"]},
            "side": "BUY",
            "$or": [
                {"idempotency_key": {"$exists": False}},
                {"idempotency_key": {"$not": {"$regex": "^exit:"}}},
            ],
        })
    except Exception:
        today_entries = 0

    # Loss-streak throttle
    try:
        streak_doc = await db.strategy_loss_streaks.find_one(
            {"strategy_id": strategy_id, "user_id": user_id},
            {"_id": 0},
        )
    except Exception:
        streak_doc = None

    if streak_doc:
        streak = int(streak_doc.get("consecutive_sl_count") or 0)
        paused_until_str = streak_doc.get("paused_until")
        if streak >= LOSS_STREAK_TRIGGER and paused_until_str:
            try:
                paused_until = datetime.fromisoformat(str(paused_until_str).replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < paused_until:
                    return False, f"LOSS_STREAK_THROTTLE: {streak} consecutive SL hits — paused until {paused_until.strftime('%H:%M')} IST"
            except Exception:
                pass
        # Half-cap after streak (even after pause ends)
        if LOSS_STREAK_HALF_CAP and streak >= LOSS_STREAK_TRIGGER:
            effective_cap = max(1, effective_cap // 2)

    if today_entries >= effective_cap:
        return False, (
            f"DAILY_CAP: {today_entries}/{effective_cap} entries today "
            f"(class={cls}, base={base_cap}, freq_mult={freq_multiplier:.1f}, profit_boost={profit_boost})"
        )

    return True, None


async def record_sl_hit(db, strategy_id: str, user_id: str) -> None:
    """Call when a SL exit fires for this strategy. Updates loss-streak counter."""
    now = datetime.now(timezone.utc)
    try:
        doc = await db.strategy_loss_streaks.find_one(
            {"strategy_id": strategy_id, "user_id": user_id}, {"_id": 0}
        )
        streak = int((doc or {}).get("consecutive_sl_count") or 0) + 1
        update: Dict[str, Any] = {
            "consecutive_sl_count": streak,
            "last_sl_at": now.isoformat(),
        }
        if streak >= LOSS_STREAK_TRIGGER:
            pause_end = now + timedelta(minutes=LOSS_STREAK_PAUSE_MIN)
            update["paused_until"] = pause_end.isoformat()
            logger.warning(
                "LOSS_STREAK strategy=%s user=%s: %d consecutive SLs — "
                "throttled until %s IST",
                strategy_id, user_id, streak,
                (pause_end + timedelta(hours=5, minutes=30)).strftime("%H:%M"),
            )
        await db.strategy_loss_streaks.update_one(
            {"strategy_id": strategy_id, "user_id": user_id},
            {"$set": update},
            upsert=True,
        )
    except Exception as exc:
        logger.debug("record_sl_hit failed: %s", exc)


async def reset_streak_on_profit(db, strategy_id: str, user_id: str) -> None:
    """Reset loss streak counter on any profitable exit."""
    try:
        await db.strategy_loss_streaks.update_one(
            {"strategy_id": strategy_id, "user_id": user_id},
            {"$set": {"consecutive_sl_count": 0, "paused_until": None}},
            upsert=True,
        )
    except Exception:
        pass


async def record_strategy_filter(
    db,
    strategy_id: str,
    user_id: str,
    filter_name: str,
    reason: str,
) -> None:
    """Log every filter rejection for EOD summary and zero-signal alert."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        start, end = _today_window()
        await db.strategy_filter_log.insert_one({
            "strategy_id": strategy_id,
            "user_id": user_id,
            "filter": filter_name,
            "reason": reason,
            "created_at": now,
        })
        # Increment today's filter counter on the strategy doc
        await db.strategies.update_one(
            {"id": strategy_id},
            {
                "$inc": {f"filter_counts.{filter_name}": 1, "filter_counts.total": 1},
                "$set": {"last_filter_reason": reason, "last_filter_at": now},
            },
        )
    except Exception as exc:
        logger.debug("record_strategy_filter failed: %s", exc)


# ── Time-of-day normalized volume baseline ────────────────────────────────────

def compute_tod_volume_ratio(
    candles: list,
    current_bar_idx: int,
    lookback_sessions: int = 5,
) -> float:
    """Time-of-day normalized volume ratio.

    Compares candle[i].volume to the average volume at the same 5-min slot
    across the last N sessions. Corrects for the U-shaped intraday volume
    curve (opening and closing are always high; same-day avg is biased).

    Returns: ratio ≥ 0.0  (1.0 = average for this time slot)
    """
    if current_bar_idx < 1 or not candles:
        return 1.0

    current = candles[current_bar_idx]
    current_time = str(current.get("date") or "")[11:16]  # "HH:MM"
    current_vol = max(1.0, float(current.get("volume") or 1))

    # Gather same time slot from previous sessions
    same_slot_vols = []
    seen_days: set = set()
    current_day = str(current.get("date") or "")[:10]

    for i in range(current_bar_idx - 1, -1, -1):
        c = candles[i]
        day = str(c.get("date") or "")[:10]
        if day == current_day:
            continue
        t = str(c.get("date") or "")[11:16]
        if t == current_time:
            seen_days.add(day)
            same_slot_vols.append(max(1.0, float(c.get("volume") or 1)))
        if len(seen_days) >= lookback_sessions:
            break

    if not same_slot_vols:
        return 1.0

    avg_slot_vol = sum(same_slot_vols) / len(same_slot_vols)
    return round(current_vol / max(1.0, avg_slot_vol), 3)
