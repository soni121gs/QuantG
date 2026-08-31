"""HSI Stage 1 — Trade Attribution Engine (deterministic, no LLM).

The "why" layer of the Hermes self-improvement loop. For every CLOSED round-trip
trade it writes ONE attribution record tagging the dimensions that explain the
win/loss: bias, regime, structure, hold time, exit reason, and the R-multiple
(realized P&L ÷ planned risk). Everything downstream (grounded EOD distillation,
scored lessons, OOS validator) reads from this collection.

Design law (CLAUDE.md §12 / project_hermes_second_brain): code computes every
number; no LLM touches this file. Source of truth = `db.trades` (the canonical
round-trip row written on every full close by portfolio_ledger / spread_lifecycle),
enriched with the closed `strategy_positions` doc for structure / option_type /
risk prices / regime.

Public API
----------
- compile_trade_attribution(db, user_id, date_str)  -> int   (rows written)
- attribution_rollup(db, user_id, since, group_by)  -> list[dict]
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("trade_attribution")

# IST is UTC+5:30, no DST.
_IST_OFFSET = timedelta(hours=5, minutes=30)

_HOLD_BUCKETS = [
    (0, 5, "0-5m"),
    (5, 15, "5-15m"),
    (15, 30, "15-30m"),
    (30, 60, "30-60m"),
    (60, 120, "1-2h"),
    (120, 10 ** 9, "2h+"),
]


def _ist_day_utc_window(date_str: str) -> tuple[str, str]:
    """Return (utc_start, utc_end) ISO strings spanning one full IST calendar day."""
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    ist_start = day  # treat the date as IST midnight
    utc_start = (ist_start - _IST_OFFSET).isoformat()
    utc_end = (ist_start + timedelta(days=1) - _IST_OFFSET).isoformat()
    return utc_start, utc_end


def _to_ist(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc) + _IST_OFFSET
    except Exception:
        return None


def _hold_bucket(minutes: Optional[float]) -> str:
    if minutes is None:
        return "unknown"
    for lo, hi, label in _HOLD_BUCKETS:
        if lo <= minutes < hi:
            return label
    return "unknown"


def _time_of_day_bucket(entry_ist: Optional[datetime]) -> str:
    if entry_ist is None:
        return "unknown"
    m = entry_ist.hour * 60 + entry_ist.minute
    if m < 9 * 60 + 45:
        return "open"          # 09:15-09:45
    if m < 11 * 60:
        return "morning"       # 09:45-11:00
    if m < 13 * 60:
        return "midday"        # 11:00-13:00
    if m < 14 * 60 + 30:
        return "afternoon"     # 13:00-14:30
    return "close"             # 14:30-15:30


def _exposure_bias(pos: Dict[str, Any]) -> Optional[str]:
    """Lazy wrapper around strategy_runner._position_exposure_bias (pure function).

    Imported lazily to avoid a core -> strategy_runner import cycle at module load.
    """
    try:
        from strategy_runner import _position_exposure_bias
        return _position_exposure_bias(pos)
    except Exception:
        return None


def _planned_risk(trade: Dict[str, Any], pos: Dict[str, Any], structure: str,
                  qty: int, entry_price: float) -> Optional[float]:
    """Defined risk of the trade in rupees.

    Spreads carry an explicit max_loss (defined-risk by construction). Single-leg /
    equity risk = |entry - stop| x qty, using the absolute sl_price stamped at entry.
    Returns None when no stop was defined (cannot compute R honestly).
    """
    if structure in ("credit_spread", "debit_spread"):
        ml_total = pos.get("max_loss_total")
        if ml_total:
            return abs(float(ml_total))
        ml = pos.get("max_loss")
        if ml:
            return abs(float(ml) * max(1, qty))
        return None
    # single-leg / equity
    sl_price = pos.get("sl_price")
    if sl_price in (None, "", 0):
        risk = pos.get("tp_sl_tsl_config") or {}
        sl_price = risk.get("stoploss_price") or risk.get("stop_loss")
    if sl_price in (None, "", 0) or entry_price <= 0 or qty <= 0:
        return None
    try:
        return abs((entry_price - float(sl_price)) * qty)
    except Exception:
        return None


def _build_record(trade: Dict[str, Any], pos: Dict[str, Any], user_id: str,
                  date_str: str) -> Dict[str, Any]:
    structure = (str(trade.get("structure") or pos.get("structure") or "").lower()
                 or "single_leg")
    asset_type = trade.get("asset_type") or pos.get("asset_type") or "unknown"
    qty = int(trade.get("quantity") or trade.get("qty") or pos.get("quantity") or 0)
    entry_price = float(trade.get("entry_price") or pos.get("average_buy_price") or 0)
    exit_price = float(trade.get("exit_price") or 0)
    realized_pnl = float(trade.get("realized_pnl") if trade.get("realized_pnl") is not None
                         else trade.get("net_pnl") or 0)
    peak_pnl = None
    if pos.get("peak_pnl") is not None:
        try:
            peak_pnl = float(pos.get("peak_pnl"))
        except Exception:
            peak_pnl = None

    entry_ist = _to_ist(trade.get("entry_time") or pos.get("entry_time"))
    exit_ist = _to_ist(trade.get("exit_time") or pos.get("closed_at"))
    hold_minutes = trade.get("duration_minutes")
    if hold_minutes is None and entry_ist and exit_ist:
        hold_minutes = round((exit_ist - entry_ist).total_seconds() / 60.0, 1)

    planned_risk = _planned_risk(trade, pos, structure, qty, entry_price)
    r_multiple = None
    if planned_risk and planned_risk > 0:
        r_multiple = round(realized_pnl / planned_risk, 3)

    regime = pos.get("regime_at_entry") or pos.get("regime") or "UNKNOWN"

    return {
        "trade_id": trade.get("id"),
        "position_id": trade.get("position_id") or pos.get("id"),
        "user_id": user_id,
        "strategy_id": trade.get("strategy_id") or pos.get("strategy_id"),
        "strategy_name": pos.get("strategy_name") or trade.get("strategy_name"),
        "date_ist": date_str,
        "underlying": trade.get("underlying") or pos.get("underlying"),
        "asset_type": asset_type,
        "structure": structure,
        "option_type": pos.get("option_type"),
        "exposure_bias": _exposure_bias(pos) or "UNKNOWN",
        "regime_at_entry": str(regime).upper() if regime else "UNKNOWN",
        "entry_time": trade.get("entry_time") or pos.get("entry_time"),
        "exit_time": trade.get("exit_time") or pos.get("closed_at"),
        "hold_minutes": hold_minutes,
        "hold_bucket": _hold_bucket(hold_minutes),
        "time_of_day": _time_of_day_bucket(entry_ist),
        "exit_reason": trade.get("exit_reason") or pos.get("exit_reason") or "unknown",
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "quantity": qty,
        "realized_pnl": round(realized_pnl, 2),
        "peak_pnl": round(peak_pnl, 2) if peak_pnl is not None else None,
        "profit_giveback": round(peak_pnl - realized_pnl, 2) if peak_pnl is not None else None,
        "green_then_red": bool(peak_pnl is not None and peak_pnl > 0 and realized_pnl < 0),
        "planned_risk": round(planned_risk, 2) if planned_risk else None,
        "R_multiple": r_multiple,
        "is_win": realized_pnl > 0,
        "compiled_at": datetime.now(timezone.utc).isoformat(),
    }


async def compile_trade_attribution(db, user_id: str, date_str: str) -> int:
    """Write one attribution record per CLOSED trade for the given IST day.

    Idempotent: unique index on trade_id + upsert, so re-running an EOD (e.g. after a
    restart) never duplicates. Returns the number of trades processed.
    """
    # Ensure the idempotency guard exists (cheap, idempotent).
    try:
        await db.trade_attribution.create_index("trade_id", unique=True)
        await db.trade_attribution.create_index([("user_id", 1), ("date_ist", 1)])
    except Exception as idx_exc:
        logger.warning("trade_attribution index ensure failed: %s", idx_exc)

    utc_start, utc_end = _ist_day_utc_window(date_str)
    trades = await db.trades.find({
        "user_id": user_id,
        "$or": [
            {"exit_time": {"$gte": utc_start, "$lt": utc_end}},
            {"created_at": {"$gte": utc_start, "$lt": utc_end}},
        ],
    }).to_list(2000)

    if not trades:
        logger.info("trade_attribution: no closed trades for user=%s date=%s", user_id, date_str)
        return 0

    # Bulk-load the joined position docs in one query.
    pos_ids = [t.get("position_id") for t in trades if t.get("position_id")]
    pos_by_id: Dict[str, Dict[str, Any]] = {}
    if pos_ids:
        async for p in db.strategy_positions.find({"id": {"$in": pos_ids}}):
            pos_by_id[p.get("id")] = p

    # Resolve strategy names (neither db.trades nor the position doc stores them).
    name_by_sid: Dict[str, str] = {}
    sids = list({t.get("strategy_id") for t in trades if t.get("strategy_id")})
    if sids:
        async for s in db.strategies.find({"id": {"$in": sids}}, {"_id": 0, "id": 1, "name": 1}):
            name_by_sid[s.get("id")] = s.get("name")

    # HSI-53: tag trades on strategies with an active Hermes config change so Stage 3
    # can later score whether the CHANGE actually improved real P&L.
    lesson_by_sid: Dict[str, str] = {}
    try:
        from core.hermes_advisor import active_change_lesson_by_strategy
        lesson_by_sid = await active_change_lesson_by_strategy(db, user_id)
    except Exception as tag_exc:
        logger.debug("hermes lesson tag lookup failed: %s", tag_exc)

    written = 0
    for trade in trades:
        if not trade.get("id"):
            continue
        pos = pos_by_id.get(trade.get("position_id")) or {}
        try:
            record = _build_record(trade, pos, user_id, date_str)
            if not record.get("strategy_name"):
                record["strategy_name"] = name_by_sid.get(record.get("strategy_id")) or record.get("strategy_id")
            record["hermes_lesson_id"] = lesson_by_sid.get(record.get("strategy_id"))
            await db.trade_attribution.update_one(
                {"trade_id": record["trade_id"]},
                {"$set": record},
                upsert=True,
            )
            written += 1
        except Exception as exc:
            logger.error("trade_attribution: failed for trade=%s: %s", trade.get("id"), exc)

    logger.info("trade_attribution: wrote %d records for user=%s date=%s",
                written, user_id, date_str)
    return written


# Dimensions that attribution_rollup can group by, mapped to the stored field.
_GROUP_FIELDS = {
    "strategy": "strategy_name",
    "strategy_id": "strategy_id",
    "structure": "structure",
    "regime": "regime_at_entry",
    "exposure_bias": "exposure_bias",
    "bias": "exposure_bias",
    "exit_reason": "exit_reason",
    "hold_bucket": "hold_bucket",
    "time_of_day": "time_of_day",
    "underlying": "underlying",
    "asset_type": "asset_type",
}


async def attribution_rollup(db, user_id: str, since: str,
                             group_by: str = "structure") -> List[Dict[str, Any]]:
    """Group attribution rows on/after `since` (date_ist, 'YYYY-MM-DD') by a dimension.

    Returns one bucket per distinct value with
    {bucket, n, net_pnl, win_rate, avg_R, expectancy, avg_hold_min, wins, losses}.
    Pure deterministic aggregation — no LLM.
    """
    field = _GROUP_FIELDS.get(group_by, group_by)
    rows = await db.trade_attribution.find(
        {"user_id": user_id, "date_ist": {"$gte": since}},
        {"_id": 0},
    ).to_list(20000)

    buckets: Dict[Any, Dict[str, Any]] = {}
    for r in rows:
        key = r.get(field) or "UNKNOWN"
        b = buckets.setdefault(key, {
            "bucket": key, "n": 0, "net_pnl": 0.0, "wins": 0, "losses": 0,
            "_r_sum": 0.0, "_r_n": 0, "_hold_sum": 0.0, "_hold_n": 0,
        })
        b["n"] += 1
        pnl = float(r.get("realized_pnl") or 0)
        b["net_pnl"] += pnl
        if r.get("is_win"):
            b["wins"] += 1
        else:
            b["losses"] += 1
        if r.get("R_multiple") is not None:
            b["_r_sum"] += float(r["R_multiple"])
            b["_r_n"] += 1
        if r.get("hold_minutes") is not None:
            b["_hold_sum"] += float(r["hold_minutes"])
            b["_hold_n"] += 1

    out = []
    for b in buckets.values():
        n = b["n"]
        out.append({
            "bucket": b["bucket"],
            "n": n,
            "net_pnl": round(b["net_pnl"], 2),
            "win_rate": round(b["wins"] / n, 3) if n else 0.0,
            "avg_R": round(b["_r_sum"] / b["_r_n"], 3) if b["_r_n"] else None,
            "expectancy": round(b["net_pnl"] / n, 2) if n else 0.0,
            "avg_hold_min": round(b["_hold_sum"] / b["_hold_n"], 1) if b["_hold_n"] else None,
            "wins": b["wins"],
            "losses": b["losses"],
        })
    out.sort(key=lambda x: x["net_pnl"])  # worst first — most useful for "why did X lose"
    return out
