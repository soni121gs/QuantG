"""RES-4 — re-entry / multi-trade policy.

The existing anti-pyramiding guard (signal_manager) already blocks a SECOND
concurrent spread and already ALLOWS a fresh entry once the prior one closes — so
"re-entry after exit" is not blocked by design. What a scalper additionally needs
is a disciplined cadence so it doesn't churn: a per-day trade cap and a cooldown
after each exit (the old book churned on its own signal flips, 43/47 trades held
<5 min). This module is that policy — PURE, so the live loop and the backtester
apply identical cadence rules.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

# Seconds to wait after an exit before the same strategy/underlying may re-enter.
REENTRY_COOLDOWN_SECONDS = float(os.environ.get("REENTRY_COOLDOWN_SECONDS", "300"))
# Default per-day trade cap for a scalp strategy (None = unlimited).
SCALP_DAILY_TRADE_CAP = int(os.environ.get("SCALP_DAILY_TRADE_CAP", "6"))


def evaluate_reentry(
    *,
    has_open_position: bool,
    trades_today: int,
    daily_cap: Optional[int] = SCALP_DAILY_TRADE_CAP,
    seconds_since_last_exit: Optional[float] = None,
    cooldown_s: float = REENTRY_COOLDOWN_SECONDS,
) -> Dict[str, Any]:
    """May this strategy/underlying open a NEW trade now? Returns {allow, reason}.

    Blocks when (in priority order):
      1. a position is already open        — no pyramiding (one at a time)
      2. the per-day trade cap is reached  — cadence discipline
      3. still inside the post-exit cooldown — anti-churn
    """
    if has_open_position:
        return {"allow": False, "reason": "position already open (no pyramiding)"}
    if daily_cap is not None and int(trades_today) >= int(daily_cap):
        return {"allow": False,
                "reason": f"daily trade cap reached ({trades_today}/{daily_cap})"}
    if (seconds_since_last_exit is not None
            and float(seconds_since_last_exit) < float(cooldown_s)):
        return {"allow": False,
                "reason": (f"re-entry cooldown "
                           f"({seconds_since_last_exit:.0f}s < {cooldown_s:.0f}s)")}
    return {"allow": True, "reason": "re-entry allowed"}
