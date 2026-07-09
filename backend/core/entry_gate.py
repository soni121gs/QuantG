"""RES-2 live entry gate — apply the validated market_context filter to a
premium-selling entry at trade time.

RES-8 proved that gating the 3% OTM put spread by "IV−RV rich AND regime RANGE"
turns it from NO_EDGE_NEGATIVE into CANDIDATE_EDGE (both years positive). This
wires that gate into the live entry path for flagged strategies (QG-O1).

Two design rules:
  • PURE core (`seller_entry_gate`) so it is unit-testable and identical to the
    backtest logic.
  • FAIL-OPEN wrapper: if IV or the realized-vol history can't be read, ALLOW the
    entry (never silently stop a working strategy on a data hiccup) — the gate can
    only ever REMOVE clearly-bad entries, not add risk.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from core.market_context import vol_edge

logger = logging.getLogger("quantg.entry_gate")

RES2_GATE_MIN_EDGE = float(os.environ.get("RES2_GATE_MIN_EDGE", "0.0"))
# Known regimes that are NOT sell-safe for a premium seller (a strong directional
# move runs a sold spread toward max loss). RANGE / UNKNOWN → allow.
NON_RANGE_REGIMES = {"TREND_UP", "TREND_DOWN", "CRASH", "MELTUP", "VOLATILE"}

# Cache recent daily closes per (underlying, date) so we read the bhavcopy store
# at most once per underlying per day, not on every signal.
_rv_cache: Dict[str, Any] = {}


def seller_entry_gate(
    *,
    regime: Optional[str],
    iv: Optional[float],
    daily_closes: Sequence[float],
    min_edge: float = RES2_GATE_MIN_EDGE,
    require_range: bool = True,
) -> Dict[str, Any]:
    """Allow a premium-selling entry only when IV−RV is rich AND the regime is
    sell-safe (RANGE). FAIL-OPEN on missing vol data. Pure — no I/O."""
    ve = vol_edge(iv, daily_closes, min_points=min_edge)
    if ve.get("rich") is None:
        return {"allow": True, "reason": "vol edge unknown — fail-open", "vol_edge": ve}
    if ve.get("rich") is not True:
        return {"allow": False, "reason": f"RES2_GATE: {ve.get('reason')}", "vol_edge": ve}
    reg = str(regime or "").upper()
    if require_range and reg in NON_RANGE_REGIMES:
        return {"allow": False, "reason": f"RES2_GATE: regime {reg} not sell-safe (need RANGE)",
                "vol_edge": ve}
    return {"allow": True, "reason": "RES2_GATE: passed (rich + sell-safe)", "vol_edge": ve}


# A day-to-day gap larger than this (calendar days) is a DATA HOLE, not a holiday
# cluster — the return across it is spurious and must not enter realized vol. NSE's
# longest real gap (long weekend + festival) is ~4 days; 6 gives margin.
_MAX_CONTIGUOUS_GAP_DAYS = int(os.environ.get("RES2_RV_MAX_GAP_DAYS", "6"))


def _recent_daily_closes(underlying: str, lookback: int = 30) -> List[float]:
    """Recent daily closes from the bhavcopy store (trailing, up to yesterday —
    fine for a realized-vol measure). Cached per underlying per day.

    Gap-robust (2026-07-09): the store is backfilled in chunks and can have a
    multi-month HOLE (e.g. 2025-12-31 → 2026-07-01). Concatenating across that hole
    injects a fake ~-8% 'daily' return that blew realized vol up to ~32% and
    permanently blocked the RES2 seller gate. We keep only the CONTIGUOUS recent run
    (restart the run whenever two dated bars are >6 calendar days apart) so RV is
    measured on real consecutive sessions, never across a data hole."""
    key = f"{underlying.upper()}:{datetime.now(timezone.utc):%Y-%m-%d}"
    if key in _rv_cache:
        return _rv_cache[key]
    closes: List[float] = []
    try:
        from core.bhavcopy_store import BhavcopyStore
        candles = [c for c in (BhavcopyStore().underlying_daily(underlying.upper()) or []) if c.get("close")]
        recent = candles[-(lookback + 2):]
        run: List[float] = []
        prev_d = None
        for c in recent:
            try:
                d = datetime.strptime(str(c.get("date"))[:10], "%Y-%m-%d").date()
            except Exception:  # noqa: BLE001
                d = None
            if prev_d is not None and d is not None and (d - prev_d).days > _MAX_CONTIGUOUS_GAP_DAYS:
                run = []  # data hole → drop everything before it, restart the run
            run.append(float(c["close"]))
            prev_d = d
        closes = run
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("entry_gate: daily closes unavailable for %s: %s", underlying, exc)
    _rv_cache[key] = closes
    return closes


async def evaluate_entry_gate_live(
    *,
    underlying: str,
    regime: Optional[str],
    iv: Optional[float],
    min_edge: float = RES2_GATE_MIN_EDGE,
) -> Dict[str, Any]:
    """Live wrapper for the seller gate. Reads recent daily closes for the
    realized-vol leg. FAIL-OPEN on any error — a data problem must never block a
    strategy that would otherwise trade."""
    try:
        closes = _recent_daily_closes(underlying)
        if not closes or iv is None:
            return {"allow": True, "reason": "RES2_GATE: data unavailable — fail-open"}
        return seller_entry_gate(regime=regime, iv=iv, daily_closes=closes, min_edge=min_edge)
    except Exception as exc:  # noqa: BLE001
        logger.warning("entry_gate: evaluation failed for %s — fail-open: %s", underlying, exc)
        return {"allow": True, "reason": f"RES2_GATE: error {exc} — fail-open"}
