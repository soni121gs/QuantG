"""Adaptive capital allocator — performance + regime weighting.

get_strategy_allocation_multiplier() is the single entry point.
It returns a multiplier in [0.25, 2.0] that the risk manager applies
to the computed position size before placing an order.

Two factors are combined:
  1. Performance multiplier — based on today's last-5 fills:
       win streak >= 3  → 1.5x  (hot strategy, reward it)
       loss streak >= 2 → 0.5x  (cold strategy, protect capital)
       otherwise        → 1.0x

  2. Regime multiplier — based on market regime vs strategy type:
       see get_regime_multiplier() in market_regime.py for the table.

Final = clamp(perf_mult × regime_mult, 0.25, 2.0)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("quantg.capital_allocator")

_ALLOC_MIN = 0.25
_ALLOC_MAX = 2.0


def _today_window_utc() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    ist = now + timedelta(hours=5, minutes=30)
    midnight_ist = ist.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = midnight_ist - timedelta(hours=5, minutes=30)
    return start_utc.isoformat(), (start_utc + timedelta(days=1)).isoformat()


def _infer_strategy_type(strategy: Dict[str, Any]) -> str:
    """Infer broad strategy type for regime weighting.

    Returns one of: 'trend', 'range', 'breakout', 'neutral'.
    Reads strategy.kind, strategy.signal_type, or strategy name keywords.
    """
    kind = str(
        strategy.get("kind")
        or strategy.get("signal_type")
        or strategy.get("strategy_type")
        or ""
    ).upper()
    name = str(strategy.get("name") or "").upper()
    combined = f"{kind} {name}"

    if any(k in combined for k in ("BREAKOUT", "BREAK_OUT", "VOLATIL")):
        return "breakout"
    if any(k in combined for k in ("TREND", "EMA", "MOMENTUM", "MELTUP", "BULL", "BEAR")):
        return "trend"
    if any(k in combined for k in ("RANGE", "MEAN_REVERT", "SCALP", "REVERSAL", "BAND")):
        return "range"
    return "neutral"


async def _performance_multiplier(db, strategy_id: str, user_id: str) -> float:
    """1.5x for hot, 0.5x for cold, 1.0x neutral — based on last-5 fills today."""
    start, end = _today_window_utc()
    fills = await db.trade_fills.find(
        {
            "user_id": user_id,
            "strategy_id": strategy_id,
            "action": {"$in": ["CLOSE", "REDUCE"]},
            "created_at": {"$gte": start, "$lt": end},
        },
        {"_id": 0, "realized_pnl": 1},
        sort=[("created_at", -1)],
    ).to_list(5)

    if not fills:
        return 1.0

    # Compute streak from most-recent backwards
    win_streak = loss_streak = 0
    for f in fills:
        pnl = float(f.get("realized_pnl") or 0)
        if pnl > 0:
            if loss_streak == 0:
                win_streak += 1
            else:
                break
        elif pnl < 0:
            if win_streak == 0:
                loss_streak += 1
            else:
                break
        else:
            break  # breakeven ends streak

    if win_streak >= 3:
        return 1.5
    if loss_streak >= 2:
        return 0.5
    return 1.0


async def get_strategy_allocation_multiplier(
    db,
    strategy_id: str,
    user_id: str,
) -> float:
    """Return combined allocation multiplier in [0.25, 2.0].

    Combines performance-based and regime-based factors.
    """
    try:
        from market_regime import get_cached_regime, get_regime_multiplier

        strategy = await db.strategies.find_one(
            {"id": strategy_id, "user_id": user_id},
            {"_id": 0, "kind": 1, "signal_type": 1, "strategy_type": 1, "name": 1, "visual_config": 1},
        ) or {}

        perf_mult = await _performance_multiplier(db, strategy_id, user_id)

        # Pick regime for the strategy's own underlying — SENSEX strategies use
        # SENSEX regime, NIFTY/BANKNIFTY strategies use their own index.
        # Falls back through NIFTY → BANKNIFTY → SENSEX if the primary is uncached.
        vc = strategy.get("visual_config") or {}
        underlying = str(
            (vc.get("options") or {}).get("underlying")
            or vc.get("underlying")
            or "NIFTY"
        ).upper()
        regime_doc = (
            get_cached_regime(underlying)
            or get_cached_regime("NIFTY")
            or get_cached_regime("BANKNIFTY")
            or get_cached_regime("SENSEX")
        )
        current_regime = (regime_doc or {}).get("regime", "RANGE")
        strategy_type = _infer_strategy_type(strategy)
        regime_mult = get_regime_multiplier(strategy_type, current_regime)

        combined = max(_ALLOC_MIN, min(_ALLOC_MAX, perf_mult * regime_mult))
        logger.info(
            "[ALLOC] strategy=%s type=%s regime=%s perf=%.2f regime_mult=%.2f → final=%.2f",
            strategy_id, strategy_type, current_regime, perf_mult, regime_mult, combined,
        )
        return combined

    except Exception as exc:
        logger.warning("[ALLOC] allocation multiplier failed for %s: %s — using 1.0", strategy_id, exc)
        return 1.0
