"""Phase 2 #1 — IV-rank regime gate.

Computes IV rank / percentile from db.vix_history (India VIX daily closes) and
gates option-BUYING: don't pay up for expensive premium when IV rank is high.
Built but default OFF — rollout path is off → shadow (log would-blocks) →
enabled. High IV is *favourable* for credit spreads (#5), so the same signal
serves both sides later.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("quantg.iv_regime")

IV_RANK_GATE_ENABLED = os.environ.get("IV_RANK_GATE_ENABLED", "false").strip().lower() == "true"
IV_RANK_GATE_SHADOW = os.environ.get("IV_RANK_GATE_SHADOW", "false").strip().lower() == "true"
IV_RANK_BUY_MAX = float(os.environ.get("IV_RANK_BUY_MAX", "70"))
IV_RANK_LOOKBACK_DAYS = int(os.environ.get("IV_RANK_LOOKBACK_DAYS", "252"))
_CACHE_TTL_SEC = float(os.environ.get("IV_RANK_CACHE_TTL_SEC", "600"))

# Module-level cache — IV rank moves slowly (one new close/day + intraday VIX).
_cache: Dict[str, Any] = {"at": None, "data": None}


async def compute_iv_rank(db) -> Optional[Dict[str, Any]]:
    """Return {iv_rank, iv_percentile, current, min, max, n} or None when there
    is insufficient history. Cached for _CACHE_TTL_SEC so it is safe to call on
    every runner tick."""
    now = datetime.now(timezone.utc)
    cached_at = _cache.get("at")
    if cached_at and (now - cached_at).total_seconds() < _CACHE_TTL_SEC:
        return _cache["data"]
    try:
        rows = await (
            db.vix_history.find({}, {"_id": 0, "date": 1, "value": 1})
            .sort("date", -1)
            .to_list(IV_RANK_LOOKBACK_DAYS)
        )
    except Exception as exc:  # transient DB issue — keep last good value
        logger.debug("iv_rank vix_history read failed: %s", exc)
        return _cache.get("data")

    vals = []
    for r in rows:
        try:
            v = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    if len(vals) < 2:
        _cache.update({"at": now, "data": None})
        return None

    current = vals[0]  # rows are date-desc → first row is the latest close
    lo, hi = min(vals), max(vals)
    iv_rank = ((current - lo) / (hi - lo) * 100.0) if hi > lo else 50.0
    iv_pct = (sum(1 for v in vals if v < current) / len(vals)) * 100.0
    data = {
        "iv_rank": round(iv_rank, 1),
        "iv_percentile": round(iv_pct, 1),
        "current": round(current, 2),
        "min": round(lo, 2),
        "max": round(hi, 2),
        "n": len(vals),
    }
    _cache.update({"at": now, "data": data})
    return data


def iv_buy_gate(iv_rank: Optional[float]) -> Dict[str, Any]:
    """Decision for an option-BUYING entry given the current IV rank.

    Returns {block, shadow, reason}:
      - block  → caller must skip the entry (only when IV_RANK_GATE_ENABLED)
      - shadow → caller logs a would-block but proceeds (IV_RANK_GATE_SHADOW)
      - reason → human string, set whenever IV rank breaches the cap
    """
    if iv_rank is None or iv_rank <= IV_RANK_BUY_MAX:
        return {"block": False, "shadow": False, "reason": None}
    reason = (
        f"IV_RANK_HIGH: iv_rank={iv_rank:.0f} > {IV_RANK_BUY_MAX:.0f} "
        "— option premium expensive for buyers"
    )
    if IV_RANK_GATE_ENABLED:
        return {"block": True, "shadow": False, "reason": reason}
    if IV_RANK_GATE_SHADOW:
        return {"block": False, "shadow": True, "reason": reason}
    return {"block": False, "shadow": False, "reason": reason}
