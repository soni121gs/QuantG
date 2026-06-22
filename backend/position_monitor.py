"""Position monitor loop — extracted from server.py.

Runs every 30 seconds. For every OPEN/FILLED/PENDING_BROKER position:
  1. Fetches live LTP from Upstox V3 feed (or falls back through chain).
  2. Updates last_ltp, unrealized_pnl, ltp_source, and risk prices in DB.
  3. If in IST market hours AND exit condition is met, calls close_fn.
  4. Reverts EXITING positions stuck > 5 minutes back to OPEN.

LTP source chain (FIX 2):
  WS_CACHE       — V3 websocket tick (freshest)
  REST_FALLBACK  — Upstox REST quote API via quote_ltp_fn
  SYMBOL_LTP     — symbol+exchange lookup via get_ltp_fn
  PAPER_CACHE    — db.paper_quote_cache (REST snapshot stored earlier)
  ENTRY_PRICE_FALLBACK — position's own average_buy_price (last resort, safe)

Per-position try/except (FIX 9): a crash on one position never stops the loop.

No imports from server.py. Dependencies are injected via run_monitor_loop().
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Coroutine, Dict, Optional

from ltp_resolver import resolve_position_ltp
from core.position_lifecycle import (
    exit_reason,
    normalize_strategy_risk,
    parse_iso_dt,
    position_risk_prices,
)
from core.portfolio_ledger import get_strategy_pnl_today
from core.spread_lifecycle import (
    value_credit_spread, spread_exit_reason, close_credit_spread,
    value_debit_spread, debit_spread_exit_reason, close_debit_spread,
)

logger = logging.getLogger("quantg.position_monitor")

# LTP source labels used in DB and logs
LTP_WS_CACHE            = "WS_CACHE"
LTP_REST_FALLBACK       = "REST_FALLBACK"
LTP_SYMBOL              = "SYMBOL_LTP"
LTP_PAPER_CACHE         = "PAPER_CACHE"
LTP_ENTRY_PRICE         = "ENTRY_PRICE_FALLBACK"

# Poll interval: 5s during market hours so HFT scalpers get timely exits,
# 30s outside hours (just stale-EXITING cleanup).
_POLL_INTERVAL_IN_HOURS  = int(os.environ.get("POSITION_MONITOR_POLL_SECONDS", "5"))
_POLL_INTERVAL_OUT_HOURS = 30
# Phantom equity-quote guard: the open-auction window can yield a transient LTP
# ~½ or ~2× the real price. Feeding it into the SL/TP check fires a phantom
# "stop-loss" and the exit fills at the bad price (HDFCBANK BUY 1649 → SELL 786
# one second later, −5190, 2026-06-22). A large cap can't move beyond its NSE
# circuit band (≤20%) intraday, so reject any equity LTP that deviates more than
# this from entry and skip the exit/mark this cycle rather than act on bad data.
_EQUITY_LTP_MAX_DEV = float(os.environ.get("EQUITY_LTP_MAX_DEV", "0.35"))

# Force-close all positions at this IST minute-of-day (15:10 = 910 minutes)
_SQUAREOFF_MINUTE_IST = 15 * 60 + 10

# EOD aggregation fires once per day at 15:35 IST (market settled, squareoff done)
_EOD_AGGREGATION_MINUTE_IST = 15 * 60 + 35
_eod_aggregation_done_date: str | None = None  # tracks which calendar date was already run


# ── IST helpers ────────────────────────────────────────────────────────────────

def _ist_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _in_market_hours() -> bool:
    ist = _ist_now()
    if ist.weekday() >= 5:
        return False
    minutes = ist.hour * 60 + ist.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


def _squareoff_due() -> bool:
    """True when IST time has reached or passed 15:10 on a weekday."""
    ist = _ist_now()
    if ist.weekday() >= 5:
        return False
    return ist.hour * 60 + ist.minute >= _SQUAREOFF_MINUTE_IST


# ── Main loop ──────────────────────────────────────────────────────────────────

async def run_monitor_loop(
    db,
    stop_event: asyncio.Event,
    *,
    close_fn: Callable[..., Coroutine],
    quote_ltp_fn: Callable[..., Coroutine],
    get_ltp_fn: Callable[..., Coroutine],
    get_settings_fn: Callable[..., Coroutine],
) -> None:
    """
    close_fn(user_id, strategy_id, reason)  → closes all positions for the strategy
    quote_ltp_fn(user_id, instrument_key)   → float | None  (V3 feed cache → REST)
    get_ltp_fn(user_id, symbol, exchange, allow_mock, execution_broker) → float | None
    get_settings_fn(user_id)                → dict of user settings
    """
    logger.info("Position monitor started (in-hours poll=%ss)", _POLL_INTERVAL_IN_HOURS)
    while not stop_event.is_set():
        in_hours = _in_market_hours()
        try:
            await _monitor_tick(db, close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn)
        except Exception as exc:
            logger.warning("Position monitor tick error: %s", exc)
        interval = _POLL_INTERVAL_IN_HOURS if in_hours else _POLL_INTERVAL_OUT_HOURS
        slept = 0
        while not stop_event.is_set() and slept < interval:
            await asyncio.sleep(1)
            slept += 1
    logger.info("Position monitor stopped")


async def _run_eod_aggregation(db, report_date: str | None = None) -> None:
    """Aggregate an IST trading day's P&L into daily_reports for every active user.

    Called once at 15:35 IST on market days. Upserts one document per user.
    """
    global _eod_aggregation_done_date
    ist = _ist_now()
    today_str = report_date or ist.strftime("%Y-%m-%d")

    if report_date is None and _eod_aggregation_done_date == today_str:
        return  # already ran today
    if report_date is None and ist.weekday() >= 5:
        return  # skip weekends
    if report_date is None and ist.hour * 60 + ist.minute < _EOD_AGGREGATION_MINUTE_IST:
        return  # not yet 15:35

    logger.info("EOD aggregation starting for %s", today_str)

    try:
        user_ids = await db.strategy_positions.distinct("user_id")
        now_ist = ist.isoformat()

        for user_id in user_ids:
            try:
                strategies = await db.strategies.find(
                    {"user_id": user_id, "status": {"$in": ["live", "paused", "draft"]}},
                    {"_id": 0, "id": 1, "name": 1},
                ).to_list(200)

                strategy_rows = []
                total_realized = 0.0
                total_unrealized = 0.0
                total_trades = 0

                for strat in strategies:
                    sid = strat.get("id")
                    sname = strat.get("name", sid)
                    pnl_data = await get_strategy_pnl_today(db, sid, user_id, trading_date=today_str)
                    r = pnl_data.get("realized_pnl", 0.0)
                    u = pnl_data.get("unrealized_pnl", 0.0)
                    t = pnl_data.get("trade_count", 0)
                    strategy_rows.append({
                        "strategy_id": sid,
                        "name": sname,
                        "realized_pnl": r,
                        "unrealized_pnl": u,
                        "trade_count": t,
                    })
                    total_realized += r
                    total_unrealized += u
                    total_trades += t

                # Signals stats for today
                report_day = datetime.strptime(today_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                ist_start = report_day.replace(hour=9, minute=15, second=0, microsecond=0)
                ist_end   = report_day.replace(hour=15, minute=30, second=0, microsecond=0)
                utc_start = (ist_start - timedelta(hours=5, minutes=30)).isoformat()
                utc_end   = (ist_end   - timedelta(hours=5, minutes=30)).isoformat()
                signals_fired = await db.signals.count_documents({
                    "user_id": user_id,
                    "created_at": {"$gte": utc_start, "$lt": utc_end},
                })
                signals_filtered = await db.signals.count_documents({
                    "user_id": user_id,
                    "status": "FILTERED",
                    "created_at": {"$gte": utc_start, "$lt": utc_end},
                })

                profitable = [s for s in strategy_rows if s["realized_pnl"] > 0]
                losing     = [s for s in strategy_rows if s["realized_pnl"] < 0]
                best  = max(profitable, key=lambda s: s["realized_pnl"], default=None)
                worst = min(losing,     key=lambda s: s["realized_pnl"], default=None)

                doc = {
                    "user_id": user_id,
                    "date": today_str,
                    "total_realized_pnl": round(total_realized, 2),
                    "total_unrealized_pnl": round(total_unrealized, 2),
                    "trades_taken": total_trades,
                    "signals_fired": signals_fired,
                    "signals_filtered": signals_filtered,
                    "market_regime": None,
                    "best_strategy": {"name": best["name"], "pnl": best["realized_pnl"]} if best else None,
                    "worst_strategy": {"name": worst["name"], "pnl": worst["realized_pnl"]} if worst else None,
                    "strategies": strategy_rows,
                    "generated_at": now_ist,
                }
                await db.daily_reports.update_one(
                    {"user_id": user_id, "date": today_str},
                    {"$set": doc},
                    upsert=True,
                )
                logger.info(
                    "EOD aggregation user=%s date=%s realized=%.2f trades=%d",
                    user_id, today_str, total_realized, total_trades,
                )
            except Exception as user_exc:
                logger.error("EOD aggregation failed for user=%s: %s", user_id, user_exc)

        if report_date is None:
            _eod_aggregation_done_date = today_str
        logger.info("EOD aggregation complete for %s", today_str)

    except Exception as exc:
        logger.error("EOD aggregation top-level error: %s", exc)


async def _monitor_tick(db, close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn) -> None:
    in_hours = _in_market_hours()
    squareoff = _squareoff_due()

    # ── EOD aggregation (15:35 IST, once per market day) ─────────────────────
    await _run_eod_aggregation(db)

    # ── Revert positions stuck in EXITING > 5 minutes ─────────────────────────
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    stuck = await db.strategy_positions.find(
        {"status": "EXITING", "updated_at": {"$lt": stale_cutoff}},
        {"id": 1, "user_id": 1, "_id": 0},
    ).to_list(100)
    for sp in stuck:
        result = await db.strategy_positions.update_one(
            {"id": sp["id"], "status": "EXITING"},
            {"$set": {"status": "OPEN", "updated_at": datetime.now(timezone.utc).isoformat()}},
        )
        if result.modified_count:
            logger.warning("Reverted stuck EXITING position id=%s to OPEN for retry", sp["id"])

    # ── Process each open position (FIX 9: per-position try/except) ───────────
    rows = await db.strategy_positions.find(
        {"status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}},
        {"_id": 0},
    ).to_list(1000)

    for pos in rows:
        # FIX 9: isolate each position — a crash on one never stops the rest
        try:
            await _process_one_position(
                db, pos, in_hours, squareoff, close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn
            )
        except Exception as pos_exc:
            logger.error(
                "position_monitor: unhandled error on pos=%s user=%s symbol=%s: %s",
                pos.get("id"), pos.get("user_id"), pos.get("target_symbol"), pos_exc,
            )


async def _resolve_ltp(
    db,
    pos: Dict[str, Any],
    quote_ltp_fn,
    get_ltp_fn,
    get_settings_fn,
) -> tuple[Optional[float], str]:
    """
    FIX 2: Full LTP resolution chain with source tracking.

    Returns (ltp, source) where source is one of the LTP_* constants.
    NEVER returns None — falls back to entry_price if all live sources fail.
    """
    return await resolve_position_ltp(
        db, pos, quote_ltp_fn, get_ltp_fn, get_settings_fn, allow_entry_fallback=True
    )


async def _leg_ltp(user_id, leg, quote_ltp_fn) -> Optional[float]:
    """Live LTP for one spread leg via the V3 feed cache → REST chain."""
    ikey = (leg or {}).get("instrument_key")
    if not ikey:
        return None
    try:
        v = await quote_ltp_fn(user_id, ikey)
        return float(v) if v is not None and float(v) > 0 else None
    except Exception:
        return None


async def _process_spread_position(db, pos, in_hours, squareoff, quote_ltp_fn) -> None:
    """Value and exit a credit/debit spread position (two legs, one doc). Closes both
    legs atomically via spread_lifecycle. Falls back to entry premiums for the
    value update when a leg LTP is unavailable (no false exit in that case)."""
    user_id = pos.get("user_id")
    legs = pos.get("legs") or []
    short_leg = next((l for l in legs if l.get("role") == "short"), None)
    long_leg = next((l for l in legs if l.get("role") == "long"), None)
    if not short_leg or not long_leg:
        logger.warning("spread monitor: pos=%s missing legs — skipping", pos.get("id"))
        return

    short_ltp = await _leg_ltp(user_id, short_leg, quote_ltp_fn)
    long_ltp = await _leg_ltp(user_id, long_leg, quote_ltp_fn)
    have_live = short_ltp is not None and long_ltp is not None
    if short_ltp is None:
        short_ltp = float(short_leg.get("entry_price") or short_leg.get("premium") or 0)
    if long_ltp is None:
        long_ltp = float(long_leg.get("entry_price") or long_leg.get("premium") or 0)

    structure = str(pos.get("structure"))
    if structure == "debit_spread":
        v = value_debit_spread(pos, short_ltp, long_ltp)
    else:
        v = value_credit_spread(pos, short_ltp, long_ltp)

    now_str = datetime.now(timezone.utc).isoformat()
    # Status-guarded: if this position was closed concurrently (e.g. by
    # position_guardian) while we were awaiting leg LTPs above, this write
    # must not resurrect stale mark-to-market fields onto a CLOSED/EXITING doc.
    set_fields = {"spread_value": v["value"], "unrealized_pnl": v["pnl"],
                  "last_tick_at": now_str, "updated_at": now_str}
    # Refresh last_fresh_tick_at whenever BOTH legs priced live. The single-leg
    # staleness guard keys off last_fresh_tick_at; a spread never gets it
    # refreshed elsewhere (the guardian skips spreads, the single-leg monitor
    # path skips them too), so without this the field stays at entry_time and
    # any code that evaluates staleness on the spread's top-level option_type
    # force-closes it at the 300s threshold (stale-quote-protective-exit). Keep
    # it fresh so the spread is never mistaken for a dead single-leg quote.
    if have_live:
        set_fields["last_fresh_tick_at"] = now_str
    await db.strategy_positions.update_one(
        {"id": pos["id"], "user_id": user_id, "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}},
        {"$set": set_fields},
    )

    # Spreads are intraday — 15:10 IST force-close.
    if squareoff:
        logger.info("spread monitor: squareoff-1510 closing pos=%s", pos.get("id"))
        if structure == "debit_spread":
            await close_debit_spread(db, pos, reason="intraday-squareoff-1510",
                                     short_ltp=short_ltp, long_ltp=long_ltp)
        else:
            await close_credit_spread(db, pos, reason="intraday-squareoff-1510",
                                      short_ltp=short_ltp, long_ltp=long_ltp)
        return

    # No exit on entry-premium fallback (value≈credit/debit → no false trigger).
    if not in_hours or not have_live:
        return

    if structure == "debit_spread":
        reason = debit_spread_exit_reason(pos, v["value"])
        if reason:
            logger.info("spread monitor exit pos=%s reason=%s value=%.2f", pos.get("id"), reason, v["value"])
            await close_debit_spread(db, pos, reason=reason, short_ltp=short_ltp, long_ltp=long_ltp)
    else:
        reason = spread_exit_reason(pos, v["value"])
        if reason:
            logger.info("spread monitor exit pos=%s reason=%s value=%.2f", pos.get("id"), reason, v["value"])
            await close_credit_spread(db, pos, reason=reason, short_ltp=short_ltp, long_ltp=long_ltp)


async def _process_one_position(
    db, pos, in_hours, squareoff, close_fn, quote_ltp_fn, get_ltp_fn, get_settings_fn
) -> None:
    user_id = pos.get("user_id")
    sid     = pos.get("strategy_id")
    symbol  = pos.get("target_symbol") or pos.get("trading_symbol") or pos.get("symbol")
    if not user_id or not sid or not symbol:
        return

    # Phase 2 #5: credit/debit spreads have two legs and their own value/exit path.
    if str(pos.get("structure")) in ("credit_spread", "debit_spread"):
        await _process_spread_position(db, pos, in_hours, squareoff, quote_ltp_fn)
        return

    # ── 15:10 IST force-close ─────────────────────────────────────────────────
    # Intraday positions (MIS, I, CO) must be closed by 15:10 regardless of their individual
    # max_hold_minutes or SL/TP levels. Delivery and NRML positions bypass this.
    if squareoff:
        product_type = str(pos.get("product") or "MIS").upper().strip()
        if product_type in ("MIS", "I", "CO"):
            logger.info(
                "position_monitor: squareoff-1510 firing for pos=%s user=%s symbol=%s",
                pos.get("id"), user_id, symbol,
            )
            await close_fn(user_id, sid, reason="intraday-squareoff-1510")
            return
        else:
            logger.debug(
                "position_monitor: skipping squareoff-1510 for overnight pos=%s product=%s symbol=%s",
                pos.get("id"), product_type, symbol,
            )

    # ── Stamp default SL/TP on UNPROTECTED positions (F7 fix) ────────────────
    # Positions created without stop_loss/take_profit in the intent are stamped
    # UNPROTECTED by the ledger. Without these, exit_reason() returns None and
    # SL/TP exits never fire. Assign defaults from DEFAULT_STRATEGY_RISK now.
    risk_cfg = pos.get("tp_sl_tsl_config") or {}
    if not risk_cfg.get("stop_loss_pct") and not risk_cfg.get("stoploss_price"):
        entry_price = float(pos.get("average_buy_price") or pos.get("average_price") or 0)
        if entry_price > 0:
            default_sl_pct = 15.0  # Widened from 8%; Phase 4 ExitPolicy replaces at entry.
            default_tp_pct = 25.0  # Widened from 12%; keeps 1.67R ratio.
            risk_cfg = dict(risk_cfg)
            risk_cfg["stop_loss_pct"] = default_sl_pct
            risk_cfg["take_profit_pct"] = default_tp_pct
            risk_cfg["protection_status"] = "PROTECTED_DEFAULTS"
            await db.strategy_positions.update_one(
                {"id": pos["id"], "user_id": user_id},
                {"$set": {"tp_sl_tsl_config": risk_cfg,
                          "updated_at": datetime.now(timezone.utc).isoformat()}},
            )
            pos = {**pos, "tp_sl_tsl_config": risk_cfg}
            logger.warning(
                "position_monitor: UNPROTECTED pos=%s user=%s symbol=%s — "
                "assigned default SL=%.0f%% TP=%.0f%%. Check order path for missing stop_loss.",
                pos.get("id"), user_id, symbol, default_sl_pct, default_tp_pct,
            )

    # ── Fetch LTP with full fallback chain ────────────────────────────────────
    ltp, ltp_source = await _resolve_ltp(db, pos, quote_ltp_fn, get_ltp_fn, get_settings_fn)

    # ── Phantom equity-quote guard ────────────────────────────────────────────
    # Reject an implausible equity LTP (suspected bad open-auction tick) before it
    # can trigger a phantom SL/TP exit or stamp a phantom P&L mark.
    if (
        ltp is not None
        and ltp_source != LTP_ENTRY_PRICE
        and str(pos.get("asset_type") or "").lower() == "equity"
    ):
        _entry_ref = float(pos.get("average_buy_price") or pos.get("average_price") or pos.get("entry_price") or 0)
        if _entry_ref > 0 and abs(float(ltp) / _entry_ref - 1.0) > _EQUITY_LTP_MAX_DEV:
            _dev = abs(float(ltp) / _entry_ref - 1.0)
            logger.warning(
                "position_monitor: rejecting phantom equity ltp=%.2f for %s (entry=%.2f dev=%.0f%% src=%s) — skipping exit/mark",
                float(ltp), symbol, _entry_ref, _dev * 100, ltp_source,
            )
            await db.strategy_positions.update_one(
                {"id": pos["id"], "user_id": user_id, "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}},
                {"$set": {"last_error": f"PHANTOM_LTP_REJECTED: {float(ltp):.2f} vs entry {_entry_ref:.2f} ({_dev * 100:.0f}%)",
                          "updated_at": datetime.now(timezone.utc).isoformat()}},
            )
            return

    # ── Staleness protective exit (TASK-P-EX02) ───────────────────────────────
    # Spreads are excluded: they have a top-level option_type ("CE"/"PE") that
    # would trip this single-leg guard, but no top-level instrument_key to price.
    # They are valued/exited by _process_spread_position (two-leg REST pricing)
    # and must never be force-closed on single-leg staleness.
    is_spread = (
        str(pos.get("structure") or "") in ("credit_spread", "debit_spread")
        or str(pos.get("asset_type") or "").lower() == "option_spread"
    )
    is_option_or_equity = (not is_spread) and (
        str(pos.get("asset_type") or "").lower() in ("option", "equity")
        or pos.get("exchange") in ("NFO", "BFO", "NSE", "BSE")
        or str(pos.get("trading_symbol") or "").endswith(("CE", "PE"))
        or str(pos.get("option_type") or "").upper() in ("CE", "PE")
        or "_EQ|" in str(pos.get("instrument_key") or "")
    )
    now_str = datetime.now(timezone.utc).isoformat()
    if is_option_or_equity and pos.get("status") in ("OPEN", "FILLED"):
        last_fresh_str = pos.get("last_fresh_tick_at") or pos.get("entry_time") or pos.get("created_at")
        last_fresh_dt = parse_iso_dt(last_fresh_str)
        if last_fresh_dt:
            elapsed = (datetime.now(timezone.utc) - last_fresh_dt).total_seconds()
            from config import MONITOR
            stale_threshold = getattr(MONITOR, "OPTION_LTP_STALE_EXIT_SECONDS", 300)
            if elapsed > stale_threshold:
                if ltp_source in ("ENTRY_PRICE_FALLBACK", "NONE") or ltp is None:
                    fresh_ltp = None
                    ikey = pos.get("instrument_key") or pos.get("instrument_token")
                    from ltp_resolver import should_trust_ws_cache
                    if ikey and should_trust_ws_cache(ikey):
                        try:
                            fresh_ltp = await quote_ltp_fn(user_id, ikey)
                        except Exception as e:
                            logger.warning("stale check REST fallback failed for %s: %s", symbol, e)
                    if fresh_ltp is not None:
                        ltp = float(fresh_ltp)
                        ltp_source = "REST_FALLBACK"
                    else:
                        logger.warning(
                            "position_monitor: OPEN position %s user=%s symbol=%s has no fresh LTP for %.0fs — forcing protective exit.",
                            pos["id"], user_id, symbol, elapsed,
                        )
                        await close_fn(user_id, sid, reason="stale-quote-protective-exit", ltp_source=ltp_source, decided_ltp=ltp)
                        return

    if ltp is None:
        # All sources exhausted — mark unavailable and skip exit check
        await db.strategy_positions.update_one(
            {"id": pos["id"], "user_id": user_id},
            {"$set": {
                "last_ltp": "LTP_UNAVAILABLE",
                "ltp_source": "NONE",
                "last_error": "LTP_UNAVAILABLE: all sources exhausted (WS/REST/cache/entry_price)",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }},
        )
        logger.warning(
            "position_monitor: all LTP sources exhausted pos=%s user=%s symbol=%s — no exit check",
            pos["id"], user_id, symbol,
        )
        return

    # ── Update LTP, P&L, and risk prices ──────────────────────────────────────
    ltp   = float(ltp)
    entry = float(pos.get("average_buy_price") or 0)
    qty   = int(pos.get("open_quantity") or pos.get("quantity") or 0)
    side  = str(pos.get("position_side") or "LONG").upper()
    pnl   = round((entry - ltp) * qty, 2) if side == "SHORT" else round((ltp - entry) * qty, 2)

    risk_prices = position_risk_prices(pos, ltp=ltp)
    risk = normalize_strategy_risk(pos.get("tp_sl_tsl_config") or {})
    if risk_prices.get("stop_loss") is not None:
        risk["stoploss_price"] = risk_prices["stop_loss"]
        risk["stop_loss"]      = risk_prices["stop_loss"]
    if risk_prices.get("take_profit") is not None:
        risk["target_price"] = risk_prices["take_profit"]
        risk["take_profit"]  = risk_prices["take_profit"]
    if risk_prices.get("trailing_sl") is not None:
        risk["trailing_sl"] = risk_prices["trailing_sl"]

    now_str = datetime.now(timezone.utc).isoformat()
    set_fields = {
        "last_ltp":       ltp,
        "ltp_source":     ltp_source,
        "unrealized_pnl": pnl,
        "last_tick_at":   now_str,
        "updated_at":     now_str,
        "tp_sl_tsl_config": risk,
    }
    if ltp_source not in ("ENTRY_PRICE_FALLBACK", "NONE"):
        set_fields["last_fresh_tick_at"] = now_str

    # Status-guarded: same race as the spread mark-to-market write above — if
    # close_fn (guardian or this same loop) already moved this position out of
    # PENDING_BROKER/OPEN/FILLED while we awaited the LTP lookup, this becomes
    # a no-op instead of stamping stale ltp/pnl onto a CLOSED/EXITING doc.
    await db.strategy_positions.update_one(
        {"id": pos["id"], "user_id": user_id, "status": {"$in": ["PENDING_BROKER", "OPEN", "FILLED"]}},
        {
            "$set": set_fields,
            "$unset": {"last_error": ""},
        },
    )

    # ── Exit check (only during market hours) ─────────────────────────────────
    if not in_hours:
        return

    # Don't trigger SL/TP exits using entry-price fallback — P&L is always 0
    # so no real exit condition can fire. Time exits are still valid.
    if ltp_source == LTP_ENTRY_PRICE:
        reason = exit_reason(pos, ltp)
        if reason and "TIME" not in reason.upper():
            # SL/TP exit with stale price — skip to avoid false trigger
            logger.debug(
                "position_monitor: skipping %s exit for %s (ltp_source=ENTRY_PRICE_FALLBACK)",
                reason, symbol,
            )
            return

    reason = exit_reason(pos, ltp)
    if reason:
        logger.info(
            "Monitor exit strategy=%s symbol=%s reason=%s ltp=%.2f source=%s",
            sid, symbol, reason, ltp, ltp_source,
        )
        await close_fn(user_id, sid, reason=reason, ltp_source=ltp_source, decided_ltp=ltp)
