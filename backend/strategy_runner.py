"""Background strategy runner.

Single asyncio task started on app startup. Every TICK_SECONDS, iterates
through all strategies with status="live", fetches a short price history,
runs the user's `run(data)` function and acts on the last signal.

Multi-replica safety: uses a Mongo-based distributed lock with TTL. Only
the pod currently holding the lock runs the runner — prevents duplicate
order placement when the deployment scales to >1 replica.

For safety: this runner ALWAYS goes through `/orders` business logic which
respects user.paper_mode and user.max_position_size — so flipping the
master switch on the profile is honoured automatically.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

from pymongo import ReturnDocument

from safe_exec import safe_run_strategy
from market_protection import MarketTrendAnalyzer
from core.market_clock import is_trading_session_active
from core.option_selector_v2 import select_option_contract
from market_regime import update_regime, get_cached_regime
from trade_frequency import check_frequency_gate, record_strategy_filter, compute_tod_volume_ratio
from iv_regime import compute_iv_rank, iv_buy_gate, IV_RANK_GATE_ENABLED, IV_RANK_GATE_SHADOW
from order_flow import orderflow_imbalance, orderflow_gate, ORDERFLOW_GATE_ENABLED, ORDERFLOW_GATE_SHADOW

logger = logging.getLogger("quantg.runner")

TICK_SECONDS = int(os.environ.get("STRATEGY_RUNNER_TICK_SECONDS", "15"))
# Min confidence bonus applied to signals aligned with CRASH/MELTUP regime
REGIME_ALIGNED_CONFIDENCE_BONUS = float(os.environ.get("REGIME_ALIGNED_CONFIDENCE_BONUS", "10.0"))
LOCK_TTL_SECONDS = 90  # lock auto-expires if a pod dies
LOCK_ID = "strategy_runner"
POD_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
SIGNAL_CONFIDENCE_MIN = float(os.environ.get("SIGNAL_CONFIDENCE_MIN", "45"))
# Phantom equity-candle guard: the open-auction window can emit a single bad
# candle ~½ or ~2× the real price. Entering off it opens at a phantom basis that
# books a huge fake loss on exit (TCS entry 4041 vs real ~2136, 2026-06-22). Skip
# the entry when the latest equity close deviates more than this from the recent
# median — well beyond any NSE large-cap circuit band (≤20%), so no real move trips it.
EQUITY_PHANTOM_MAX_DEV = float(os.environ.get("EQUITY_PHANTOM_MAX_DEV", "0.35"))
# Signal-exit min-hold debounce: a fresh position must not be closed by its own
# opposite-direction signal before this many seconds — a real SL/TP exit (owned by
# the position monitor) still fires anytime. Spreads need a long hold to collect
# theta (the 2026-06-23 churn: 43/47 spreads closed <5min, never reached TP);
# single-leg buyers get a short debounce just to stop same-candle flip-flop.
SPREAD_SIGNAL_EXIT_MIN_HOLD_SEC = int(os.environ.get("SPREAD_SIGNAL_EXIT_MIN_HOLD_SEC", "1200"))
SINGLE_LEG_SIGNAL_EXIT_MIN_HOLD_SEC = int(os.environ.get("SINGLE_LEG_SIGNAL_EXIT_MIN_HOLD_SEC", "180"))
NON_ERROR_ENTRY_BLOCKS = (
    "cooldown-active",
    "duplicate-buy-dropped",
    "max-trades-day-reached",
    "New BUY blocked",
    "Duplicate BUY blocked",
    "Re-entry blocked",
    "already has active",
    "Live LTP unavailable",
    "Insufficient funds",
    "insufficient funds",
    "margin",
    "Margin",
)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: int) -> None:
    slept = 0
    while not stop_event.is_set() and slept < seconds:
        await asyncio.sleep(1)
        slept += 1


def _enrich_tod_ratios(data: List[dict]) -> List[dict]:
    """Pre-compute time-of-day normalized volume ratio for each candle.

    Attaches 'tod_vol_ratio' to each candle dict so strategy code strings can
    access float(data[i].get('tod_vol_ratio', 1.0)) without needing an import.
    Shallow-copies each dict to avoid mutating cached candle objects.
    """
    for i in range(len(data)):
        data[i] = dict(data[i])
        data[i]['tod_vol_ratio'] = compute_tod_volume_ratio(data, i)
    return data


def _safe_run(code: str, data: List[dict]) -> List[dict]:
    """Run user strategy via shared AST-validated sandbox. Returns [] on error."""
    try:
        return safe_run_strategy(code, data)
    except Exception as e:
        logger.warning(f"strategy code error: {e}")
        return []


def _validate_signal(signal: Dict[str, Any], data: List[dict], strategy: Dict[str, Any] = None) -> Dict[str, Any]:
    """Return signal diagnostics without blocking strategy decisions.

    The strategy owns trading logic and strategy guards. The runner only needs a
    normalized confidence/trend envelope for UI/debugging before queuing the
    signal for platform execution checks.
    """
    try:
        trend = MarketTrendAnalyzer.analyze(data, lookback=min(50, max(20, len(data))))
        confidence = signal.get("confidence")
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 85.0
        # Enforce the minimum-confidence gate (SIGNAL_CONFIDENCE_MIN, default 45).
        # This was previously declared but never applied — every signal passed.
        if confidence < SIGNAL_CONFIDENCE_MIN:
            return {
                "is_valid": False,
                "confidence": confidence,
                "threshold": SIGNAL_CONFIDENCE_MIN,
                "reasons": [f"confidence {confidence:.1f} below minimum {SIGNAL_CONFIDENCE_MIN:.1f}"],
                "filtered": True,
                "trend": trend,
            }
        return {
            "is_valid": True,
            "confidence": confidence,
            "threshold": SIGNAL_CONFIDENCE_MIN,
            "reasons": [],
            "filtered": False,
            "trend": trend,
            "platform_note": "strategy-owned-signal-accepted-for-platform-preflight",
        }
    except Exception as e:
        logger.warning(f"signal validation failed: {e}")
        return {
            "is_valid": True,
            "confidence": 85.0,
            "threshold": None,
            "reasons": [f"Diagnostics failed: {e}"],
            "filtered": False,
            "trend": {},
        }


def _entry_block_reason(exc: Exception) -> str | None:
    status_code = getattr(exc, "status_code", None)
    detail = str(getattr(exc, "detail", "") or exc)
    if status_code not in (None, 400, 409):
        return None
    if not any(prefix in detail or prefix in str(exc) for prefix in (
        "Option entry blocked:",
        "Strategy entry blocked:",
        "Instrument already has active strategy position:",
        "Strategy already has active position",
        "Instrument/strategy already reserved",
        "Live LTP unavailable",
        "Insufficient funds",
        "insufficient funds",
        "margin",
        "Margin",
    )):
        return None
    for reason in NON_ERROR_ENTRY_BLOCKS:
        if reason in detail or reason in str(exc):
            return reason
    return None


def _latest_signal_price(signal: Dict[str, Any], data: List[dict], option_contract: Dict[str, Any] | None = None) -> float | None:
    """Best available price for the unified execution preflight."""
    for candidate in (
        (option_contract or {}).get("ltp"),
        signal.get("price"),
        signal.get("ltp"),
        signal.get("close"),
        signal.get("entry_price"),
        (data[-1] if data else {}).get("close"),
        (data[-1] if data else {}).get("price"),
        (data[-1] if data else {}).get("ltp"),
    ):
        try:
            value = float(candidate)
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue
    return None


def _select_latest_recent_signal(
    signals: List[Dict[str, Any]],
    data: List[dict],
    lookback_candles: int = 2,
) -> Dict[str, Any] | None:
    recent_dates = {d.get("date") for d in data[-lookback_candles:]}
    for signal in reversed(signals):
        if signal.get("date", "") in recent_dates:
            return signal
    return None


def _select_paper_measurement_signal(signals: List[Dict[str, Any]], data: List[dict]) -> Dict[str, Any] | None:
    if not data:
        return None
    latest_session = str(data[-1].get("date") or "")[:10]
    recent_dates = {d.get("date") for d in data[-12:]}
    for signal in reversed(signals):
        signal_date = str(signal.get("date") or "")
        if signal_date[:10] == latest_session and signal_date in recent_dates:
            return signal
    return None


def _paper_measurement_reason(signal: Dict[str, Any], data: List[dict]) -> str:
    return (
        "Paper measurement mode: accepted same-session setup "
        f"from {signal.get('date')!r} within the last {min(12, len(data))} candles."
    )


def _contract_resolution_update(
    eval_set: Dict[str, Any],
    inc_set: Dict[str, Any],
    action: str,
    signals_count: int,
    clear_reason: str,
    is_paper_mode: bool,
    diagnostics: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    diagnostics = diagnostics or {}
    update_set = {
        **eval_set,
        "last_error": None if is_paper_mode else clear_reason,
        "last_filter_reason": clear_reason,
        "last_signals_count": signals_count,
        "last_signal_action": action,
        "last_resolver_stage": diagnostics.get("resolver_stage") or diagnostics.get("stage"),
        "last_resolver_reason": diagnostics.get("resolver_reason") or diagnostics.get("reason") or clear_reason,
        "last_instrument_source": diagnostics.get("instrument_source"),
        "last_instrument_key": diagnostics.get("instrument_key"),
        "last_quote_source": diagnostics.get("quote_source"),
        "last_quote_age_sec": diagnostics.get("quote_age_sec"),
        "subscribed_key": diagnostics.get("subscribed_key"),
        "cache_lookup_key": diagnostics.get("cache_lookup_key"),
        "cache_hit": diagnostics.get("cache_hit"),
        "quote_timestamp": diagnostics.get("quote_timestamp"),
        "quote_reject_reason": diagnostics.get("quote_reject_reason"),
    }
    update_doc: Dict[str, Any] = {"$set": update_set, "$inc": inc_set}
    update_doc["$set"].update({
        "halted": False,
        "is_halted": False,
        "last_skip_reason_code": "CONTRACT_RESOLUTION_FAILED",
    })
    update_doc["$unset"] = {"halt_reason": "", "last_halt_reason": ""}
    return update_doc


async def _acquire_lock(db) -> bool:
    """Try to grab the runner lock. Returns True if this pod owns it now."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=LOCK_TTL_SECONDS)
    try:
        # 1. Try to take an expired/abandoned lock or refresh our own
        row = await db.runner_locks.find_one_and_update(
            {
                "_id": LOCK_ID,
                "$or": [
                    {"expires_at": {"$lt": now}},
                    {"owner": POD_ID},
                ],
            },
            {"$set": {"owner": POD_ID, "expires_at": expires_at, "renewed_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if row:
            return True
        # 2. No existing lock at all — try to create one. If another pod creates
        #    it first we'll hit a duplicate-key error which we treat as "not us".
        try:
            await db.runner_locks.insert_one({
                "_id": LOCK_ID, "owner": POD_ID,
                "expires_at": expires_at, "renewed_at": now,
            })
            return True
        except Exception:
            # Another pod has the lock — perfectly fine, we'll retry next tick.
            return False
    except Exception as e:
        logger.warning(f"lock acquire failed: {e}")
        return False


async def _release_lock(db) -> None:
    try:
        await db.runner_locks.delete_one({"_id": LOCK_ID, "owner": POD_ID})
    except Exception:
        pass


# Tracks the last known regime per underlying to detect mid-session flips
_last_regime_per_index: Dict[str, str] = {}


async def _tighten_positions_on_regime_flip(
    db, new_regime: dict, all_user_ids: list
) -> None:
    """On CRASH or MELTUP flip, tighten against-regime open positions.

    CRASH → LONG positions are against regime.
    MELTUP → SHORT positions are against regime.

    Rules:
      - unrealized P&L >= 0.5 × initial_risk_abs → move SL to entry (breakeven lock)
      - otherwise → halve the remaining time to deadline
    """
    r_name = new_regime.get("regime", "")
    if r_name not in ("CRASH", "MELTUP"):
        return

    against_side = "LONG" if r_name == "CRASH" else "SHORT"
    now = datetime.now(timezone.utc)

    query: Dict[str, Any] = {"status": "OPEN", "position_side": against_side}
    if all_user_ids:
        query["user_id"] = {"$in": all_user_ids}

    try:
        positions = await db.strategy_positions.find(query).to_list(200)
    except Exception as exc:
        logger.warning("regime_flip tighten: DB query failed: %s", exc)
        return

    for pos in positions:
        pos_id = pos.get("id")
        entry = float(pos.get("average_buy_price") or pos.get("entry_price") or 0)
        ltp = float(pos.get("last_ltp") or entry)
        risk_abs = float(pos.get("r_initial_risk_amount") or 0)
        if entry <= 0:
            continue
        qty = int(pos.get("open_quantity") or 1)
        pnl = (ltp - entry) * qty if against_side == "LONG" else (entry - ltp) * qty

        update: Dict[str, Any] = {
            "updated_at": now.isoformat(),
            "regime_flip_tightened": True,
        }
        if risk_abs > 0 and pnl >= 0.5 * risk_abs:
            update["sl_price"] = entry
            update["tp_sl_tsl_config.stoploss_price"] = entry
            logger.info(
                "regime_flip %s: pos %s breakeven lock at %.2f (pnl=%.2f, 0.5R=%.2f)",
                r_name, pos_id, entry, pnl, 0.5 * risk_abs,
            )
        else:
            deadline_str = pos.get("deadline_at")
            if deadline_str:
                try:
                    deadline = datetime.fromisoformat(
                        str(deadline_str).replace("Z", "+00:00")
                    )
                    remaining = (deadline - now).total_seconds()
                    if remaining > 60:
                        new_deadline = now + timedelta(seconds=remaining / 2)
                        update["deadline_at"] = new_deadline.isoformat()
                        logger.info(
                            "regime_flip %s: pos %s deadline halved to %s",
                            r_name, pos_id, new_deadline.isoformat(),
                        )
                except Exception:
                    pass

        if len(update) > 2:
            try:
                await db.strategy_positions.update_one({"id": pos_id}, {"$set": update})
            except Exception as exc:
                logger.warning("regime_flip tighten update failed for %s: %s", pos_id, exc)


async def runner_loop(db, get_price_history, place_order_fn, stop_event: asyncio.Event,
                      resolve_option_fn=None, close_strategy_fn=None):
    """Main loop. Dependencies injected to avoid circular imports.

    resolve_option_fn(user_id, underlying, signal_action, strike_mode, otm_points,
                      expiry_offset) -> contract_dict | None
    When provided and a strategy has visual_config.options.enabled=True, signals
    are translated to option contracts and place_order_fn is called with
    option_contract kwarg.
    """
    logger.info(f"Strategy runner starting (tick={TICK_SECONDS}s, pod={POD_ID})")
    while not stop_event.is_set():
        # Global market hours gate: do nothing outside 9:00 AM – 3:35 PM IST Mon–Fri.
        # This prevents all DB reads/writes and lock acquisition when the market is closed.
        if not is_trading_session_active():
            await _sleep_or_stop(stop_event, TICK_SECONDS)
            continue
        owns_lock = await _acquire_lock(db)
        if not owns_lock:
            # Another pod is leader; skip this tick.
            await _sleep_or_stop(stop_event, TICK_SECONDS)
            continue
        try:
            strategies = await db.strategies.find({"status": "live"}).to_list(500)
        except Exception as e:
            logger.exception(f"runner loop error fetching strategies: {e}")
            strategies = []
        for idx, s in enumerate(strategies):
            # Renew the distributed lock every 20 strategies to prevent TTL expiry
            # during a large batch (90s TTL can expire if each strategy takes ~450ms).
            if idx > 0 and idx % 20 == 0:
                renewed = await _acquire_lock(db)
                if not renewed:
                    logger.warning("runner lost lock mid-batch at strategy index %d — continuing without lock renewal", idx)
            try:
                code = s.get("python_code") or ""
                eval_set: Dict[str, Any] = {
                    "last_evaluated_at": datetime.now(timezone.utc).isoformat(),
                    "last_pod": POD_ID,
                }
                # increment scan counter (cumulative + per-day, reset by daily lifecycle)
                inc_set: Dict[str, Any] = {"evaluations": 1, "evaluations_today": 1}
                if not code:
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": eval_set, "$inc": inc_set})
                    continue
                # Resolve the symbol whose PRICE HISTORY the strategy will analyse.
                # When options mode is enabled, we MUST evaluate the strategy
                # against the UNDERLYING's spot price (NIFTY/BANKNIFTY/SENSEX) —
                # not against an unrelated equity symbol like RELIANCE. The
                # equity `symbol` field is only used in equity mode.
                vc = s.get("visual_config") or {}

                opt_cfg_early = (vc or {}).get("options") or {}
                if opt_cfg_early.get("enabled"):
                    symbol = (opt_cfg_early.get("underlying") or "NIFTY").upper()
                else:
                    raw_sym = vc.get("symbol")
                    if not raw_sym:
                        # No symbol configured — skip with a clear error rather than silently
                        # evaluating against a wrong default. This prevents RELIANCE candles
                        # being used as a proxy for an unrelated strategy.
                        await db.strategies.update_one({"id": s["id"]},
                                                       {"$set": {**eval_set, "last_error": "No symbol configured. Set a trading symbol in strategy settings to enable scanning."},
                                                        "$inc": inc_set})
                        continue
                    symbol = raw_sym.upper()
                # Enforce market-hours check for this symbol segment
                try:
                    from core.market_domains import resolve_domain_by_underlying, DomainType
                    from core.market_clock import get_segment_status
                    domain = resolve_domain_by_underlying(symbol)
                    # domain.name is the DomainType enum member (e.g. DomainType.NSE_FO).
                    # get_segment_status expects a DomainType enum value, not a string.
                    clock = get_segment_status(domain.name)
                    if not clock.get("open"):
                        # Skip running strategy outside market hours to avoid spamming failed orders
                        await db.strategies.update_one(
                            {"id": s["id"]},
                            {"$set": {**eval_set,
                                      "last_filter_reason": f"Market closed: {clock.get('reason')}"},
                             "$inc": inc_set}
                        )
                        continue
                except Exception as e:
                    logger.warning(f"Market hours check failed for {symbol}: {e}")
                # last 60 daily candles for context
                try:
                    history = await get_price_history(s["user_id"], symbol, days=60, strategy=s)
                except Exception as e:
                    logger.warning(f"price history failed for {symbol}: {e}")
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set, "last_error": str(e)[:200]},
                                                    "$inc": inc_set})
                    continue
                data = history
                if isinstance(history, dict):
                    data = history.get("data") or []
                    eval_set["last_data_source"] = history.get("source", "unknown")
                    eval_set["last_data_live"] = bool(history.get("is_live"))
                    eval_set["last_data_reason"] = history.get("live_reason")
                    eval_set["last_candle_at"] = history.get("last_candle_at")
                    eval_set["latest_candle_age_sec"] = history.get("latest_candle_age_sec")
                if not data:
                    error = None
                    if isinstance(history, dict) and not history.get("paper_mode", True):
                        source = history.get("source", "none")
                        error = f"No real Upstox price history available yet (source={source}). Reconnect Upstox and restart the feed."
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set, "last_error": error}, "$inc": inc_set})
                    continue
                data = _enrich_tod_ratios(data)
                signals = _safe_run(code, data)
                signals_count = len(signals)
                if not signals:
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {
                                                       **eval_set,
                                                       "last_signals_count": 0,
                                                       "last_filter_reason": (
                                                           f"No current setup from strategy code "
                                                           f"(candles={len(data)}, source={eval_set.get('last_data_source', 'unknown')})."
                                                       ),
                                                   },
                                                    "$inc": inc_set})
                    continue
                is_paper_mode = bool(history.get("paper_mode", True)) if isinstance(history, dict) else False
                if not is_paper_mode and not bool(history.get("is_live") if isinstance(history, dict) else False):
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set,
                                                             "last_error": "Mock price history; live strategy execution blocked until real Upstox data is available.",
                                                             "last_signals_count": len(signals)},
                                                    "$inc": inc_set})
                    continue
                last_sig = _select_latest_recent_signal(signals, data)
                paper_measurement_reason = None
                if not last_sig and is_paper_mode:
                    last_sig = _select_paper_measurement_signal(signals, data)
                    if last_sig:
                        paper_measurement_reason = _paper_measurement_reason(last_sig, data)
                if not last_sig:
                    last_historical_date = (signals[-1] or {}).get("date", "")
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set,
                                                             "last_signals_count": signals_count,
                                                             "last_filter_reason": (
                                                                 f"No current setup from strategy code "
                                                                 f"(candles={len(data)}, source={eval_set.get('last_data_source', 'unknown')}; "
                                                                 f"last historical signal={last_historical_date!r})."
                                                             )},
                                                    "$inc": inc_set})
                    continue
                last_sig_date = last_sig.get("date", "")
                last_fired_date = s.get("last_fired_signal_date", "")

                # Don't re-fire the same signal we already acted on
                if last_sig_date and last_sig_date == last_fired_date:
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set,
                                                             "last_signals_count": signals_count,
                                                             "last_filter_reason": "Duplicate signal (already fired for this candle)"},
                                                    "$inc": inc_set})
                    continue

                action = (last_sig.get("action") or "").upper()
                if action not in ("BUY", "SELL"):
                    await db.strategies.update_one({"id": s["id"]},
                                                   {"$set": {**eval_set, "last_signals_count": signals_count},
                                                    "$inc": inc_set})
                    continue
                signal_validation = _validate_signal(last_sig, data, s)
                if not signal_validation.get("is_valid"):
                    reason = "; ".join(signal_validation.get("reasons") or [])
                    await db.strategies.update_one(
                        {"id": s["id"]},
                        {"$set": {**eval_set,
                                  "last_signals_count": signals_count,
                                  "last_signal_action": action,
                                  "last_filter_reason": (
                                      f"Signal filtered: confidence {signal_validation.get('confidence')} "
                                      f"< {signal_validation.get('threshold')}. {reason}"
                                  ),
                                  "last_signal_validation": signal_validation},
                         "$unset": {"last_error": ""},
                         "$inc": inc_set},
                    )
                    continue
                # ── Regime Gate ───────────────────────────────────────────────
                # Update regime from latest candles (CRASH/MELTUP checked every
                # tick; full bias refresh every 15 min inside update_regime).
                _underlying = str(
                    ((vc or {}).get("options") or {}).get("underlying") or symbol
                ).upper()
                try:
                    _regime = await update_regime(db, _underlying, data)
                except Exception:
                    _regime = get_cached_regime(_underlying) or {}

                # Detect mid-session regime flip and tighten against-regime positions
                _prev_regime = _last_regime_per_index.get(_underlying)
                _curr_regime = _regime.get("regime", "RANGE")
                if _prev_regime and _prev_regime != _curr_regime:
                    _all_uids = list({_s.get("user_id") for _s in strategies if _s.get("user_id")})
                    asyncio.ensure_future(
                        _tighten_positions_on_regime_flip(db, _regime, _all_uids)
                    )
                _last_regime_per_index[_underlying] = _curr_regime

                _long_ok  = _regime.get("long_entries_allowed", True)
                _short_ok = _regime.get("short_entries_allowed", True)
                _is_entry = action in ("BUY", "SELL") and not any(
                    kw in (last_sig.get("entry_reason") or "").lower()
                    for kw in ("exit", "time exit", "squareoff", "close")
                )

                # ── Exit-signal position reconciliation ───────────────────────
                # An exit signal (e.g. "Macro EMA cross exit") only makes sense if
                # the strategy actually holds a position. The position monitor runs
                # its own TP/SL/time exits, so by the time the strategy's exit
                # signal fires the position is often already closed. Letting a
                # position-less exit flow on to order placement is the root cause of
                # two bugs: a SELL becomes a rejected DUPLICATE_EXIT ("NEEDS CHECK"
                # noise), and a BUY hits the ledger's create-new path and opens a
                # PHANTOM LONG. The option-buying path already reconciles against an
                # active position; this makes every path (incl. equity) do the same.
                if not _is_entry:
                    _open_pos = await db.strategy_positions.find_one(
                        {
                            "user_id": s["user_id"],
                            "strategy_id": s["id"],
                            "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
                        },
                        {"_id": 0, "id": 1},
                    )
                    if not _open_pos:
                        await db.strategies.update_one(
                            {"id": s["id"]},
                            {"$set": {**eval_set,
                                      "last_signals_count": signals_count,
                                      "last_signal_action": action,
                                      "last_filter_reason": "Exit signal with no open position — suppressed (already closed by monitor or never opened)."},
                             "$unset": {"last_error": ""},
                             "$inc": inc_set},
                        )
                        continue

                if _is_entry:
                    # Determine option direction: v13 signals carry explicit "direction"
                    # field ("CE"=bullish exposure, "PE"=bearish exposure).
                    # Legacy signals use action as proxy (BUY=bullish, SELL=bearish).
                    _sig_dir = (last_sig.get("direction") or "").upper()
                    _is_ce_exposure = ("CE" in _sig_dir) or (action == "BUY" and not _sig_dir)
                    _is_pe_exposure = ("PE" in _sig_dir) or (action == "SELL" and not _sig_dir)

                    if _is_ce_exposure and not _long_ok:
                        _reason = f"REGIME_GATE: {_regime.get('regime','?')} — CE/long exposure blocked"
                        await record_strategy_filter(db, s["id"], s.get("user_id"), "REGIME_GATE", _reason)
                        await db.strategies.update_one(
                            {"id": s["id"]},
                            {"$set": {**eval_set, "last_signals_count": signals_count,
                                      "last_signal_action": action,
                                      "last_filter_reason": _reason},
                             "$inc": inc_set},
                        )
                        continue
                    if _is_pe_exposure and not _short_ok:
                        _reason = f"REGIME_GATE: {_regime.get('regime','?')} — PE/short exposure blocked"
                        await record_strategy_filter(db, s["id"], s.get("user_id"), "REGIME_GATE", _reason)
                        await db.strategies.update_one(
                            {"id": s["id"]},
                            {"$set": {**eval_set, "last_signals_count": signals_count,
                                      "last_signal_action": action,
                                      "last_filter_reason": _reason},
                             "$inc": inc_set},
                        )
                        continue
                    # Confidence bonus for regime-aligned entries
                    _r_name = _regime.get("regime", "RANGE")
                    if (_r_name in ("CRASH", "TREND_DOWN") and _is_pe_exposure) or \
                       (_r_name in ("MELTUP", "TREND_UP") and _is_ce_exposure):
                        last_sig = dict(last_sig)
                        last_sig["confidence"] = min(100.0, float(last_sig.get("confidence") or 85.0) + REGIME_ALIGNED_CONFIDENCE_BONUS)

                    # S5 strict gate: block entries in RANGE regime
                    if last_sig.get("regime_gate_strict"):
                        if _r_name == "RANGE":
                            _reason = f"REGIME_STRICT_GATE: RANGE regime — strategy requires trend"
                            if is_paper_mode:
                                last_sig = dict(last_sig)
                                last_sig["regime_strict_paper_bypassed"] = True
                                last_sig["regime_strict_reason"] = f"{_reason}; paper entry allowed for measurement."
                                paper_measurement_reason = "Paper measurement mode: strict regime gate warning logged but not blocking paper entry."
                            else:
                                await record_strategy_filter(db, s["id"], s.get("user_id"), "REGIME_STRICT_GATE", _reason)
                                await db.strategies.update_one(
                                    {"id": s["id"]},
                                    {"$set": {**eval_set, "last_signals_count": signals_count,
                                              "last_signal_action": action,
                                              "last_filter_reason": _reason},
                                     "$inc": inc_set},
                                )
                                continue

                    # S2 overextended exemption: only allow in CRASH (PE) or MELTUP (CE)
                    if last_sig.get("overextended_regime_exempt"):
                        _exempt_ok = (
                            (_r_name == "MELTUP" and action == "BUY") or
                            (_r_name == "CRASH" and action == "SELL")
                        )
                        if not _exempt_ok:
                            _reason = f"OVEREXT_GATE: overextended breakout blocked — regime={_r_name}, needs CRASH(PE)/MELTUP(CE)"
                            await record_strategy_filter(db, s["id"], s.get("user_id"), "OVEREXT_GATE", _reason)
                            await db.strategies.update_one(
                                {"id": s["id"]},
                                {"$set": {**eval_set, "last_signals_count": signals_count,
                                          "last_signal_action": action,
                                          "last_filter_reason": _reason},
                                 "$inc": inc_set},
                            )
                            continue

                    # Apply hold_multiplier to max_hold_minutes
                    _hm = float(_regime.get("hold_multiplier", 1.0))
                    if _hm != 1.0 and last_sig.get("max_hold_minutes"):
                        last_sig = dict(last_sig)
                        last_sig["max_hold_minutes"] = max(5, int(last_sig["max_hold_minutes"] * _hm))

                # ── Frequency Gate ────────────────────────────────────────────
                if _is_entry:
                    try:
                        _freq_ok, _freq_reason = await check_frequency_gate(
                            db,
                            strategy_id=s["id"],
                            strategy_name=str(s.get("name") or ""),
                            user_id=s.get("user_id"),
                            freq_multiplier=float(_regime.get("freq_multiplier", 1.0)),
                        )
                    except Exception:
                        _freq_ok, _freq_reason = True, None
                    if not _freq_ok:
                        await record_strategy_filter(db, s["id"], s.get("user_id"), "FREQ_GATE", _freq_reason or "")
                        await db.strategies.update_one(
                            {"id": s["id"]},
                            {"$set": {**eval_set, "last_signals_count": signals_count,
                                      "last_signal_action": action,
                                      "last_filter_reason": _freq_reason},
                             "$inc": inc_set},
                        )
                        continue

                # Attach regime snapshot to the signal for diagnostics
                last_sig = dict(last_sig)
                last_sig["regime_snapshot"] = _regime

                # Determine if this strategy trades OPTIONS instead of equity
                opt_cfg = (vc or {}).get("options") or {}
                option_contract = None
                instrument_type = str(
                    opt_cfg.get("instrument_type")
                    or opt_cfg.get("contract_type")
                    or s.get("instrument_type")
                    or s.get("asset_class")
                    or ""
                ).upper()
                option_resolution_requested = bool(opt_cfg.get("enabled")) and instrument_type not in {"FUTURE", "FUTURES", "FUTCOM", "COMMODITY_FUTURE"}
                option_buying_mode = option_resolution_requested and str(opt_cfg.get("strike_mode") or "").upper().endswith("BUY")
                if option_buying_mode:
                    # In option-buying strategies, exit can be SELL (for CE) or BUY (for PE)
                    active_position = await db.strategy_positions.find_one(
                        {
                            "user_id": s["user_id"],
                            "strategy_id": s["id"],
                            "status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]},
                        }
                    )
                    if active_position:
                        inst_key = str(active_position.get("instrument_key") or active_position.get("trading_symbol") or "").upper()
                        is_pe = "PE" in inst_key or inst_key.endswith("PE")
                        is_exit = (action == "SELL" and not is_pe) or (action == "BUY" and is_pe)

                        risk_cfg = (vc or {}).get("risk") or {}
                        exit_mode = risk_cfg.get("exit_mode", "tp_sl_tsl_or_signal")
                        if exit_mode == "tp_sl_only":
                            is_exit = False

                        if is_exit and close_strategy_fn:
                            # Before honoring a signal-driven exit, check if stop-loss was already hit
                            ltp = active_position.get("last_ltp")
                            try:
                                ltp_val = float(ltp) if ltp is not None else None
                            except (TypeError, ValueError):
                                ltp_val = None

                            exit_tag = f"strategy-{action.lower()}-signal"
                            sl_triggered = False
                            if ltp_val is not None:
                                from core.position_lifecycle import position_risk_prices
                                prices = position_risk_prices(active_position, ltp=ltp_val)
                                stop_loss = prices.get("stop_loss")
                                trailing_sl = prices.get("trailing_sl")
                                side = str(active_position.get("position_side") or "LONG").upper()
                                if stop_loss is not None:
                                    is_triggered = (ltp_val >= stop_loss) if side == "SHORT" else (ltp_val <= stop_loss)
                                    if is_triggered:
                                        exit_tag = "trailing-sl" if trailing_sl else "stop-loss"
                                        sl_triggered = True

                            # Min-hold debounce: suppress a PURE opposite-signal exit on a
                            # fresh position so spreads ride to theta TP instead of churning.
                            # A real SL/TP exit (sl_triggered) is never blocked — the monitor
                            # owns those, so risk is always honored.
                            if not sl_triggered:
                                _is_spread = str(active_position.get("structure") or "") in ("credit_spread", "debit_spread")
                                _min_hold = SPREAD_SIGNAL_EXIT_MIN_HOLD_SEC if _is_spread else SINGLE_LEG_SIGNAL_EXIT_MIN_HOLD_SEC
                                _opened_raw = active_position.get("entry_time") or active_position.get("created_at")
                                _opened_dt = None
                                if isinstance(_opened_raw, str):
                                    try:
                                        _opened_dt = datetime.fromisoformat(_opened_raw.replace("Z", "+00:00"))
                                    except ValueError:
                                        _opened_dt = None
                                elif isinstance(_opened_raw, datetime):
                                    _opened_dt = _opened_raw
                                if _opened_dt is not None:
                                    if _opened_dt.tzinfo is None:
                                        _opened_dt = _opened_dt.replace(tzinfo=timezone.utc)
                                    _held_sec = (datetime.now(timezone.utc) - _opened_dt).total_seconds()
                                    if _held_sec < _min_hold:
                                        await db.strategies.update_one(
                                            {"id": s["id"]},
                                            {"$set": {**eval_set,
                                                      "last_signal_action": action,
                                                      "last_signals_count": signals_count,
                                                      "last_filter_reason": (
                                                          f"{action} signal exit suppressed — min-hold "
                                                          f"{int(_held_sec)}s/{_min_hold}s ({'spread' if _is_spread else 'single-leg'}); "
                                                          f"letting TP/SL ride."
                                                      )},
                                             "$inc": inc_set},
                                        )
                                        continue

                            await close_strategy_fn(s["user_id"], s["id"], reason=exit_tag)
                            await db.strategies.update_one(
                                {"id": s["id"]},
                                {"$set": {**eval_set,
                                          "last_signal_action": action,
                                          "last_signals_count": signals_count,
                                          "last_filter_reason": f"{action} signal used as option-buying exit (exited as {exit_tag})."},
                                 "$inc": inc_set},
                            )
                        else:
                            # Active position already exists for this strategy — block new same-direction entry.
                            # Without this guard a new candle BUY signal while already long would open a second position.
                            await db.strategies.update_one(
                                {"id": s["id"]},
                                {"$set": {**eval_set,
                                          "last_signals_count": signals_count,
                                          "last_filter_reason": f"Active position exists — skipping new {action} entry"},
                                 "$inc": inc_set},
                            )
                        continue
                if option_resolution_requested and resolve_option_fn:
                    try:
                        option_contract = await resolve_option_fn(
                            user_id=s["user_id"],
                            underlying=opt_cfg.get("underlying", "NIFTY"),
                            signal_action=action,
                            strike_mode=opt_cfg.get("strike_mode", "ATM_BUY"),
                            otm_points=int(opt_cfg.get("otm_points") or 0),
                            expiry_offset=int(opt_cfg.get("expiry_offset") or 0),
                            strategy=s,
                        )
                        if not option_contract:
                            underlying_name = opt_cfg.get("underlying", "NIFTY")
                            resolver_diag = getattr(resolve_option_fn, "last_diagnostics", {}) if resolve_option_fn else {}
                            clear_reason = resolver_diag.get("resolver_reason") or resolver_diag.get("reason") or f"Upstox option contract resolution failed for {underlying_name}."
                            update_doc = _contract_resolution_update(
                                eval_set=eval_set,
                                inc_set=inc_set,
                                action=action,
                                signals_count=signals_count,
                                clear_reason=clear_reason,
                                is_paper_mode=is_paper_mode,
                                diagnostics=resolver_diag,
                            )
                            await db.strategies.update_one({"id": s["id"]}, update_doc)
                            continue

                        # ---- OptionSelector v2 quality gate ----
                        _v2_direction = str(last_sig.get("direction") or "CE").upper()
                        _v2_preference = str(last_sig.get("option_selection_preference") or "ATM").upper()
                        _v2_mode = "paper" if is_paper_mode else "live"
                        _v2_spot = float(option_contract.get("spot") or 0)
                        _v2_underlying = str(opt_cfg.get("underlying", "NIFTY")).upper()
                        _v2_confidence = float(last_sig.get("confidence") or 85.0)

                        _v2_result = select_option_contract(
                            underlying=_v2_underlying,
                            direction=_v2_direction,
                            preference=_v2_preference,
                            mode=_v2_mode,
                            signal_confidence=_v2_confidence,
                            spot=_v2_spot,
                            resolved_contract=option_contract,
                        )

                        if not _v2_result["ok"]:
                            _skip_reason = _v2_result["reason_code"]
                            logger.warning(
                                "option_selector_v2 blocked signal strategy=%s underlying=%s direction=%s reason=%s",
                                s["id"], _v2_underlying, _v2_direction, _skip_reason,
                            )
                            # Store diagnostic telemetry for the status endpoint
                            try:
                                await db.option_selector_decisions.insert_one({
                                    "strategy_id": s["id"],
                                    "user_id": s["user_id"],
                                    "underlying": _v2_underlying,
                                    "direction": _v2_direction,
                                    "preference": _v2_preference,
                                    "selected_contract": None,
                                    "selected_strike_mode": None,
                                    "quality_score": 0,
                                    "reason_code": _skip_reason,
                                    "mode": _v2_mode,
                                    "created_at": datetime.now(timezone.utc).isoformat(),
                                })
                            except Exception:
                                pass
                            await db.strategies.update_one(
                                {"id": s["id"]},
                                {"$set": {
                                    **eval_set,
                                    "last_signals_count": signals_count,
                                    "last_signal_action": action,
                                    "last_filter_reason": f"{_skip_reason}: option quality gate blocked signal.",
                                },
                                 "$inc": inc_set},
                            )
                            continue

                        # Attach v2 metadata to the resolved contract
                        option_contract = _v2_result["selected_contract"]
                        option_contract["option_quality_score"] = _v2_result["quality_score"]
                        option_contract["selected_strike_mode"] = _v2_result["selected_strike_mode"]
                        option_contract["v2_warnings"] = _v2_result.get("warnings", [])

                        # Store telemetry for diagnostics endpoint
                        try:
                            await db.option_selector_decisions.insert_one({
                                "strategy_id": s["id"],
                                "user_id": s["user_id"],
                                "underlying": _v2_underlying,
                                "direction": _v2_direction,
                                "preference": _v2_preference,
                                "selected_contract": {
                                    "instrument_key": option_contract.get("instrument_key"),
                                    "tradingsymbol": option_contract.get("tradingsymbol"),
                                    "strike": option_contract.get("strike"),
                                    "ltp": option_contract.get("ltp"),
                                },
                                "selected_strike_mode": _v2_result["selected_strike_mode"],
                                "quality_score": _v2_result["quality_score"],
                                "reason_code": _v2_result["reason_code"],
                                "mode": _v2_mode,
                                "created_at": datetime.now(timezone.utc).isoformat(),
                            })
                        except Exception:
                            pass
                        # ---- end OptionSelector v2 gate ----

                    except Exception as e:
                        logger.warning(f"option resolve failed for {s['id']}: {e}")
                        await db.strategies.update_one(
                            {"id": s["id"]},
                            {"$set": {**eval_set, "last_error": str(e)[:200],
                                      "last_signals_count": signals_count},
                             "$inc": inc_set})
                        # CRITICAL: option resolution failed — do NOT fall through and
                        # queue a signal with option_contract=None/stale. That is how
                        # un-closeable ghost positions were created.
                        continue

                # ── Phase 4: Theta guard + ATR-based ExitPolicy ───────────────
                # Only applies to option-buying entries (not exits/time-exits).
                # Credit spreads are EXEMPT: they are net-credit (option SELLING),
                # so low vol / theta works in their favour — they have their own
                # TP/SL via spread_lifecycle and must not be theta-blocked.
                _is_credit_spread = str((option_contract or {}).get("structure")) == "credit_spread"
                if option_buying_mode and _is_entry and option_contract and not _is_credit_spread:
                    try:
                        from exit_policy import attach_exit_policy_to_signal
                        _opt_ltp = float(
                            option_contract.get("ltp") or option_contract.get("price") or 0
                        )
                        if _opt_ltp > 0 and data:
                            last_sig = attach_exit_policy_to_signal(last_sig, data, _opt_ltp)
                            if is_paper_mode and last_sig.get("theta_guard_blocked"):
                                _atr_val = last_sig.get("underlying_atr_pct", 0.0)
                                last_sig["theta_guard_paper_bypassed"] = True
                                last_sig["theta_guard_reason"] = (
                                    f"THETA_GUARD: atr%={_atr_val:.4f} below minimum threshold; paper entry allowed for measurement."
                                )
                                last_sig.pop("theta_guard_blocked", None)
                                if not paper_measurement_reason:
                                    paper_measurement_reason = "Paper measurement mode: theta guard warning logged but not blocking paper entry."
                            if last_sig.get("theta_guard_blocked"):
                                _atr_val = last_sig.get("underlying_atr_pct", 0.0)
                                _tg_reason = (
                                    f"THETA_GUARD: atr%={_atr_val:.4f} below minimum threshold "
                                    "— option-buying blocked (premium decay exceeds expected move)"
                                )
                                await record_strategy_filter(
                                    db, s["id"], s.get("user_id"), "THETA_GUARD", _tg_reason
                                )
                                await db.strategies.update_one(
                                    {"id": s["id"]},
                                    {"$set": {**eval_set,
                                              "last_signals_count": signals_count,
                                              "last_signal_action": action,
                                              "last_filter_reason": _tg_reason},
                                     "$inc": inc_set},
                                )
                                continue
                    except Exception as _ep_exc:
                        logger.debug("exit_policy attach failed for %s: %s", s["id"], _ep_exc)

                # ── Phase 2 #1: IV-rank regime gate (option buyers) ───────────
                # Built but default OFF. Shadow mode logs would-blocks without
                # acting; enabled mode skips the entry. Zero overhead when off.
                if (IV_RANK_GATE_ENABLED or IV_RANK_GATE_SHADOW) and option_buying_mode and _is_entry:
                    try:
                        _ivr = await compute_iv_rank(db)
                        _ivd = iv_buy_gate((_ivr or {}).get("iv_rank"))
                        if _ivd["shadow"]:
                            logger.info("IV_RANK_WOULD_BLOCK strategy=%s %s", s["id"], _ivd["reason"])
                            await record_strategy_filter(db, s["id"], s.get("user_id"), "IV_RANK_SHADOW", _ivd["reason"])
                        elif _ivd["block"]:
                            await record_strategy_filter(db, s["id"], s.get("user_id"), "IV_RANK_GATE", _ivd["reason"])
                            await db.strategies.update_one(
                                {"id": s["id"]},
                                {"$set": {**eval_set,
                                          "last_signals_count": signals_count,
                                          "last_signal_action": action,
                                          "last_filter_reason": _ivd["reason"]},
                                 "$inc": inc_set},
                            )
                            continue
                    except Exception as _iv_exc:
                        logger.debug("iv_rank gate failed for %s: %s", s["id"], _iv_exc)

                # ── Phase 2 #3: order-flow confirmation (option buyers) ───────
                # Built but default OFF. Requires net buying pressure (tbq/tsq)
                # on the contract being bought. Shadow logs would-blocks only.
                if (ORDERFLOW_GATE_ENABLED or ORDERFLOW_GATE_SHADOW) and option_buying_mode and _is_entry and option_contract:
                    try:
                        _imb = orderflow_imbalance(option_contract)
                        _ofd = orderflow_gate(_imb, "BUY")
                        if _ofd["shadow"]:
                            logger.info("ORDERFLOW_WOULD_BLOCK strategy=%s %s", s["id"], _ofd["reason"])
                            await record_strategy_filter(db, s["id"], s.get("user_id"), "ORDERFLOW_SHADOW", _ofd["reason"])
                        elif _ofd["block"]:
                            await record_strategy_filter(db, s["id"], s.get("user_id"), "ORDERFLOW_GATE", _ofd["reason"])
                            await db.strategies.update_one(
                                {"id": s["id"]},
                                {"$set": {**eval_set,
                                          "last_signals_count": signals_count,
                                          "last_signal_action": action,
                                          "last_filter_reason": _ofd["reason"]},
                                 "$inc": inc_set},
                            )
                            continue
                    except Exception as _of_exc:
                        logger.debug("orderflow gate failed for %s: %s", s["id"], _of_exc)

                # ── Phantom equity-candle guard (entry-side) ──────────────────
                # Reject an equity entry whose latest candle close is an outlier
                # vs the recent median — a suspected bad open-auction tick that
                # would open the position at a phantom basis. Equity only; option
                # and spread entries price off the chain, not the underlying bar.
                if _is_entry and str(s.get("asset_class") or "").lower() == "equity" and len(data) >= 6:
                    try:
                        _last_close = float((data[-1] or {}).get("close") or 0)
                        _prior = sorted(c for c in (float((d or {}).get("close") or 0) for d in data[-6:-1]) if c > 0)
                        if _last_close > 0 and len(_prior) >= 3:
                            _med = _prior[len(_prior) // 2]
                            if _med > 0 and abs(_last_close / _med - 1.0) > EQUITY_PHANTOM_MAX_DEV:
                                _ph_reason = (
                                    f"PHANTOM_CANDLE: last close {_last_close:.2f} deviates "
                                    f"{abs(_last_close / _med - 1.0) * 100:.0f}% from recent median {_med:.2f} "
                                    "— skipping entry on suspected bad open-auction tick"
                                )
                                logger.warning("phantom-candle guard: strategy=%s %s", s["id"], _ph_reason)
                                await record_strategy_filter(db, s["id"], s.get("user_id"), "PHANTOM_CANDLE", _ph_reason)
                                await db.strategies.update_one(
                                    {"id": s["id"]},
                                    {"$set": {**eval_set,
                                              "last_signals_count": signals_count,
                                              "last_signal_action": action,
                                              "last_filter_reason": _ph_reason},
                                     "$inc": inc_set},
                                )
                                continue
                    except Exception as _ph_exc:
                        logger.debug("phantom-candle guard failed for %s: %s", s["id"], _ph_exc)

                # Insert signal into db.signals collection instead of placing order directly
                try:
                    target_symbol = option_contract["tradingsymbol"] if option_contract else symbol
                    option_type = option_contract.get("option_type") if option_contract else None
                    signal_price = _latest_signal_price(last_sig, data, option_contract)
                    signal_id = str(uuid.uuid4())
                    now_str = datetime.now(timezone.utc).isoformat()
                    
                    signal_doc = {
                        "id": signal_id,
                        "user_id": s["user_id"],
                        "strategy_id": s["id"],
                        "mode": "paper" if is_paper_mode else "live",
                        "symbol": symbol,
                        "target_symbol": target_symbol,
                        "option_type": option_type,
                        "action": action,
                        "confidence": float(last_sig.get("confidence") or signal_validation.get("confidence", 85.0)),
                        "price": signal_price,
                        "ltp": signal_price,
                        "trend_context": signal_validation.get("trend") or {},
                        "visual_config": s.get("visual_config") or {},
                        "option_contract": option_contract,
                        "exchange": (option_contract.get("exchange") if option_contract else "NSE"),
                        "status": "PENDING",
                        "rejection_reason": None,
                        "order_id": None,
                        "created_at": now_str,
                        "processed_at": None,
                        "setup_type": last_sig.get("setup_type") or "breakout",
                        "entry_reason": last_sig.get("entry_reason") or last_sig.get("reason") or "Legacy entry signal",
                        "target_R": float(last_sig.get("target_R") or 2.0),
                        "initial_stop_R": float(last_sig.get("initial_stop_R") or 1.0),
                        "trail_after_R": float(last_sig.get("trail_after_R") or 1.5),
                        "max_hold_minutes": int(last_sig.get("max_hold_minutes") or 60),
                        "invalidation_rule": last_sig.get("invalidation_rule") or "time_or_stop",
                        "regime_required": last_sig.get("regime_required") or "any",
                        "option_selection_preference": last_sig.get("option_selection_preference") or "ATM",
                        "signal_version": last_sig.get("signal_version") or "v13",
                        "strategy_logic_version": last_sig.get("strategy_logic_version") or "1.0",
                        "default_strategy_version": s.get("default_strategy_version") or "v13-live-brain-r1",
                        # OptionSelector v2 metadata
                        "option_quality_score": (option_contract or {}).get("option_quality_score"),
                        "selected_strike_mode": (option_contract or {}).get("selected_strike_mode"),
                        "v2_selector_warnings": (option_contract or {}).get("v2_warnings"),
                        # Regime snapshot at signal time
                        "regime_snapshot": last_sig.get("regime_snapshot") or {},
                        "regime": (last_sig.get("regime_snapshot") or {}).get("regime"),
                        # Greeks/IV/OI snapshot at signal time (flat copy of the
                        # contract fields for analytics queries; the full contract
                        # is embedded above in option_contract)
                        "greeks_at_signal": {
                            k: (option_contract or {}).get(k)
                            for k in ("iv", "oi", "delta", "theta", "gamma", "vega", "rho",
                                      "bid", "ask", "bid_qty", "ask_qty", "tbq", "tsq")
                            if (option_contract or {}).get(k) is not None
                        } or None,
                        # Phase 4: ATR-based exit policy (overrides percentage defaults in position_guardian)
                        "exit_policy": last_sig.get("exit_policy"),
                        "underlying_atr_pct": last_sig.get("underlying_atr_pct"),
                    }
                    
                    await db.signals.insert_one(signal_doc)
                    
                    await db.strategies.update_one(
                        {"id": s["id"]},
                        {"$set": {**eval_set,
                                  "last_signal_action": action,
                                  "last_signals_count": signals_count,
                                  **({"last_filter_reason": paper_measurement_reason} if paper_measurement_reason else {}),
                                  "last_fired_signal_date": last_sig_date,
                                  "last_traded_symbol": target_symbol,
                                  "last_resolver_stage": (option_contract or {}).get("resolver_stage"),
                                  "last_resolver_reason": (option_contract or {}).get("resolver_reason"),
                                  "last_instrument_source": (option_contract or {}).get("source"),
                                  "last_instrument_key": (option_contract or {}).get("instrument_key"),
                                  "last_quote_source": (option_contract or {}).get("quote_source"),
                                  "last_quote_age_sec": (option_contract or {}).get("quote_age_sec"),
                                  "subscribed_key": (option_contract or {}).get("subscribed_key"),
                                  "cache_lookup_key": (option_contract or {}).get("cache_lookup_key"),
                                  "cache_hit": (option_contract or {}).get("cache_hit"),
                                  "quote_timestamp": (option_contract or {}).get("quote_timestamp"),
                                  "quote_reject_reason": (option_contract or {}).get("quote_reject_reason")},
                         "$inc": {**inc_set, "signals_fired": 1}},
                    )
                    logger.info(f"strategy {s['id']} → queued PENDING {action} signal {signal_id} for {target_symbol}")
                except Exception as e:
                    logger.warning(f"Failed to queue signal for strategy {s['id']}: {e}")
                    await db.strategies.update_one(
                        {"id": s["id"]},
                        {"$set": {**eval_set, "last_error": str(e)[:200]},
                         "$inc": inc_set})
            except Exception as e:
                logger.warning(f"strategy {s.get('id','?')} eval failed: {e}")
                try:
                    await db.strategies.update_one(
                        {"id": s.get("id")},
                        {"$set": {**eval_set, "last_error": str(e)[:200]},
                         "$inc": inc_set},
                    )
                except Exception:
                    pass
        await _sleep_or_stop(stop_event, TICK_SECONDS)
    # Cleanup: release lock so another pod can take over immediately
    await _release_lock(db)
    logger.info("Strategy runner stopped")
