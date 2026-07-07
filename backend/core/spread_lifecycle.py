"""Phase 2 #5 — credit-spread lifecycle (open / value / close).

A defined-risk vertical credit spread is stored as ONE strategy_positions doc
with two embedded legs (short + long), so it opens and closes atomically and can
never leave a naked short. This module is an isolated accounting path parallel
to PaperAdapter+PortfolioLedger (which are single-leg) — it reuses the same
PaperWallet and charge model but writes the parent+legs itself.

Sign conventions (per unit = premium points; ×lot_size×lots for money):
  - net_credit = short_premium - long_premium   (received at open; > 0)
  - spread value to close = short_ltp - long_ltp (the net debit to exit)
  - pnl_per_unit = net_credit - value           (profit when value shrinks)
  - max_loss = width - net_credit               (defined risk)

Exit (TP 50% credit / SL 2x credit, both capped by the spread width):
  - tp_value = net_credit * (1 - TP_FRAC)       (close once 50% captured)
  - sl_value = min(net_credit * (1 + SL_MULT), width)
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core.paper_broker import PaperWallet

logger = logging.getLogger("quantg.spread_lifecycle")

SPREAD_TP_FRAC = float(os.environ.get("CREDIT_SPREAD_TP_FRAC", "0.5"))   # capture 50% of credit/profit
SPREAD_SL_MULT = float(os.environ.get("CREDIT_SPREAD_SL_MULT", "2.0"))   # stop at 2x credit loss
DEBIT_SPREAD_SL_FRAC = float(os.environ.get("DEBIT_SPREAD_SL_FRAC", "0.5"))  # exit once 50% of debit is lost


def _leg_charges(side: str, price: float, qty: int) -> float:
    """Upstox-like paper charge model (mirrors PaperAdapter)."""
    gross = abs(float(price) * int(qty))
    brokerage = min(20.0, gross * 0.0003)
    stt = gross * (0.000625 if str(side).upper() == "SELL" else 0.0)
    exchange_txn = gross * 0.00053
    sebi = gross * 0.000001
    stamp = gross * (0.00003 if str(side).upper() == "BUY" else 0.0)
    gst = (brokerage + exchange_txn + sebi) * 0.18
    return round(brokerage + stt + exchange_txn + sebi + stamp + gst, 2)


def value_credit_spread(position: Dict[str, Any], short_ltp: float, long_ltp: float) -> Dict[str, float]:
    """Current spread value (net debit to close) and unrealized P&L."""
    qty = int(position.get("open_quantity") or position.get("quantity") or 0)
    net_credit = float(position.get("net_credit") or position.get("average_buy_price") or 0.0)
    value = round(float(short_ltp) - float(long_ltp), 2)
    pnl = round((net_credit - value) * qty, 2)
    return {"value": value, "pnl": pnl, "net_credit": net_credit}


def spread_exit_reason(position: Dict[str, Any], value: float) -> Optional[str]:
    """TP/SL on the spread value. Returns reason or None."""
    tp_value = position.get("spread_tp_value")
    sl_value = position.get("spread_sl_value")
    if tp_value is not None and value <= float(tp_value):
        return "spread-tp"
    if sl_value is not None and value >= float(sl_value):
        return "spread-sl"
    return None


def compute_exit_levels(net_credit: float, width: float) -> Dict[str, float]:
    tp_value = round(net_credit * (1.0 - SPREAD_TP_FRAC), 2)
    sl_value = round(min(net_credit * (1.0 + SPREAD_SL_MULT), float(width)), 2)
    return {"spread_tp_value": tp_value, "spread_sl_value": sl_value}


async def open_credit_spread(
    db,
    *,
    user_id: str,
    strategy_id: str,
    underlying: str,
    spread: Dict[str, Any],
    lots: int,
    lot_size: int,
    mode: str = "paper",
    idempotency_key: str,
    signal_id: Optional[str] = None,
    regime_at_entry: str = "UNKNOWN",
) -> Dict[str, Any]:
    """Open a credit spread as one position with embedded legs (paper only).

    Wallet: SELL short leg credits proceeds, BUY long leg debits cost — net
    effect ≈ +net_credit×qty − charges. Both legs are filled together; if funds
    are insufficient for the long leg the whole spread is rejected (no naked short).
    """
    if mode != "paper":
        return {"ok": False, "status": "SKIPPED", "reason": "credit spreads are paper-only for now"}
    if not spread.get("ok"):
        return {"ok": False, "status": "SKIPPED", "reason": spread.get("reason") or "invalid spread"}

    # Idempotency: one spread per entry key.
    existing = await db.strategy_positions.find_one(
        {"user_id": user_id, "idempotency_key": idempotency_key,
         "status": {"$in": ["OPEN", "EXITING", "CLOSED"]}},
        {"_id": 0, "id": 1},
    )
    if existing:
        return {"ok": False, "status": "SKIPPED", "reason": "duplicate spread idempotency", "id": existing["id"]}

    qty = max(1, int(lots)) * max(1, int(lot_size))
    short = dict(spread["short_leg"])
    long = dict(spread["long_leg"])
    net_credit = float(spread["net_credit"])
    width = float(spread["width_points"])
    max_loss = float(spread["max_loss"])

    wallet = PaperWallet(db)
    pos_id = f"pos_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    short_charges = _leg_charges("SELL", short["premium"], qty)
    long_charges = _leg_charges("BUY", long["premium"], qty)
    long_cost = round(long["premium"] * qty + long_charges, 2)
    short_proceeds = round(short["premium"] * qty - short_charges, 2)

    # Fund the protective (long) leg first; reject the whole spread if short.
    funded = await wallet.debit(user_id, long_cost, f"{pos_id}:long:open")
    if not funded:
        return {"ok": False, "status": "SKIPPED", "reason": "insufficient paper funds for spread long leg"}
    await wallet.credit(user_id, short_proceeds, f"{pos_id}:short:open")

    entry_charges = round(short_charges + long_charges, 2)
    levels = compute_exit_levels(net_credit, width)

    for leg, side in ((short, "SELL"), (long, "BUY")):
        leg["qty"] = qty
        leg["entry_price"] = leg["premium"]

    position_doc = {
        "id": pos_id,
        "user_id": user_id,
        "strategy_id": strategy_id,
        "symbol": underlying,
        "target_symbol": f"{underlying} {short.get('strike')}/{long.get('strike')} {spread.get('option_type')} CREDIT",
        "trading_symbol": f"{underlying}-{spread.get('direction')}-spread",
        "mode": mode,
        "structure": "credit_spread",
        "direction": spread.get("direction"),
        "option_type": spread.get("option_type"),
        "underlying": underlying,
        "symbol_group": underlying,
        "expiry": short.get("expiry"),
        "asset_type": "option_spread",
        "asset_class": "OPTION_SPREAD",
        "position_side": "SHORT",   # net short premium — profits as value decays
        "status": "OPEN",
        "legs": [short, long],
        "quantity": qty,
        "open_quantity": qty,
        "qty": qty,
        "lots": max(1, int(lots)),
        "lot_size": int(lot_size),
        "net_credit": round(net_credit, 2),
        "average_buy_price": round(net_credit, 2),  # entry "price" = credit received
        "average_price": round(net_credit, 2),
        "entry_price": round(net_credit, 2),
        "max_loss": round(max_loss, 2),
        "max_loss_total": round(max_loss * qty, 2),
        "spread_tp_value": levels["spread_tp_value"],
        "spread_sl_value": levels["spread_sl_value"],
        "net_delta": spread.get("net_delta"),
        "net_theta": spread.get("net_theta"),
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "pnl": 0.0,
        "entry_charges": entry_charges,
        "brokerage": entry_charges,
        "idempotency_key": idempotency_key,
        "signal_id": signal_id,
        "created_at": now,
        "updated_at": now,
        "entry_time": now,
        # HSI-13: exact attribution inputs stamped at creation.
        "regime_at_entry": str(regime_at_entry or "UNKNOWN").upper(),
        "planned_risk": round(max_loss * qty, 2),
    }
    await db.strategy_positions.insert_one(position_doc)

    # Live-account behaviour: block the spread's max loss as margin (a real broker
    # reserves it even though the SELL leg credited premium). Released on close.
    # Idempotent per position; only affects the "available funds" view, not P&L.
    await wallet.block_margin(user_id, round(max_loss * qty, 2), pos_id)

    # Audit: one order row per leg (mode=paper, FILLED).
    for leg in (short, long):
        await db.orders.insert_one({
            "id": f"ord_{uuid.uuid4().hex[:12]}",
            "user_id": user_id,
            "strategy_id": strategy_id,
            "position_id": pos_id,
            "symbol": underlying,
            "target_symbol": leg.get("tradingsymbol") or f"{underlying} {leg.get('strike')} {leg.get('option_type')}",
            "instrument_key": leg.get("instrument_key"),
            "side": leg["side"],
            "qty": qty,
            "lot_size": int(lot_size),
            "filled_qty": qty,
            "price": leg["premium"],
            "requested_price": leg["premium"],
            "status": "FILLED",
            "execution_status": "FILLED",
            "mode": mode,
            "broker": "paper",
            "structure": "credit_spread",
            "spread_role": leg["role"],
            "idempotency_key": f"{idempotency_key}:{leg['role']}",
            "created_at": now,
            "updated_at": now,
        })

    logger.info(
        "Spread OPEN %s %s %s credit=%.2f max_loss=%.2f qty=%d tp_val=%.2f sl_val=%.2f",
        underlying, spread.get("direction"), spread.get("option_type"),
        net_credit, max_loss, qty, levels["spread_tp_value"], levels["spread_sl_value"],
    )
    return {"ok": True, "status": "FILLED", "id": pos_id, "position_id": pos_id,
            "net_credit": net_credit, "max_loss": max_loss}


async def close_credit_spread(
    db,
    position: Dict[str, Any],
    *,
    reason: str,
    short_ltp: float,
    long_ltp: float,
) -> Dict[str, Any]:
    """Close both legs of a spread atomically and realize P&L.

    Wallet: BUY back the short (debit), SELL the long (credit). Realized P&L =
    (net_credit − close_value) × qty − round-trip charges.
    """
    pos_id = position["id"]
    user_id = position["user_id"]
    strategy_id = position["strategy_id"]
    qty = int(position.get("open_quantity") or position.get("quantity") or 0)
    net_credit = float(position.get("net_credit") or position.get("average_buy_price") or 0.0)

    # Atomically claim the close (OPEN/EXITING → CLOSED) to prevent double-close.
    now = datetime.now(timezone.utc).isoformat()
    claim = await db.strategy_positions.update_one(
        {"id": pos_id, "user_id": user_id, "status": {"$in": ["OPEN", "EXITING"]}},
        {"$set": {"status": "EXITING", "updated_at": now}},
    )
    if claim.modified_count == 0:
        return {"ok": False, "status": "SKIPPED", "reason": "spread already closing/closed"}

    short_ltp = float(short_ltp)
    long_ltp = float(long_ltp)
    close_value = round(short_ltp - long_ltp, 2)

    wallet = PaperWallet(db)
    buyback_charges = _leg_charges("BUY", short_ltp, qty)    # buy back the short
    sell_charges = _leg_charges("SELL", long_ltp, qty)       # sell the long
    exit_charges = round(buyback_charges + sell_charges, 2)

    # Fund the buy-back of the short leg; credit the long-leg sale.
    await wallet.debit(user_id, round(short_ltp * qty + buyback_charges, 2), f"{pos_id}:short:close")
    await wallet.credit(user_id, max(0.0, round(long_ltp * qty - sell_charges, 2)), f"{pos_id}:long:close")

    gross_pnl = round((net_credit - close_value) * qty, 2)
    entry_charges = float(position.get("entry_charges") or 0.0)
    net_pnl = round(gross_pnl - exit_charges - entry_charges, 2)

    closed_at = datetime.now(timezone.utc).isoformat()
    await db.strategy_positions.update_one(
        {"id": pos_id, "user_id": user_id},
        {"$set": {
            "status": "CLOSED",
            "closed_at": closed_at,
            "updated_at": closed_at,
            "open_quantity": 0,
            "exit_reason": reason,
            "exit_value": close_value,
            "realized_pnl": net_pnl,
            "pnl": net_pnl,
            "gross_pnl": gross_pnl,
            "unrealized_pnl": 0.0,
        }},
    )

    # Release the margin reserved at open (idempotent; the close was already claimed
    # atomically above, so this fires at most once per spread).
    await wallet.release_margin(user_id, pos_id)

    # Round-trip trade row + scorecard (mirrors PortfolioLedger on a full close).
    await db.trades.insert_one({
        "id": f"trade_{uuid.uuid4().hex[:12]}",
        "position_id": pos_id,
        "user_id": user_id,
        "strategy_id": strategy_id,
        "symbol": position.get("symbol"),
        "target_symbol": position.get("target_symbol"),
        "underlying": position.get("underlying"),
        "asset_type": "option_spread",
        "structure": "credit_spread",
        "mode": position.get("mode"),
        "position_side": "SHORT",
        "quantity": qty,
        "qty": qty,
        "entry_price": net_credit,
        "exit_price": close_value,
        "gross_pnl": gross_pnl,
        "charges": round(entry_charges + exit_charges, 2),
        "net_pnl": net_pnl,
        "pnl": net_pnl,
        "realized_pnl": net_pnl,
        "is_win": net_pnl > 0,
        "exit_reason": reason,
        "entry_time": position.get("entry_time"),
        "exit_time": closed_at,
        "created_at": closed_at,
    })

    # Canonical ledger row (db.trade_fills) so spread P&L is visible to the SINGLE
    # source of truth — get_strategy_pnl_today and every reader converged onto
    # trade_fills (dashboard summary, leaderboard time-brackets, scorecard). Those
    # readers filter on action in {CLOSE, REDUCE} + created_at and sum realized_pnl
    # (+ charges). Without this row, credit-spread P&L was invisible to all of them.
    # Idempotent: the close is already claimed atomically above (OPEN/EXITING →
    # CLOSED), so this runs at most once per spread; id is deterministic regardless.
    try:
        await db.trade_fills.insert_one({
            "id": f"tf_spread_{pos_id}",
            "fill_id": f"tf_spread_{pos_id}",
            # Deterministic unique order_id: trade_fills has a unique (non-sparse)
            # index on order_id, so every spread close omitting it stored null and
            # the 2nd+ close collided (E11000). Per-position id keeps cross-spread
            # uniqueness AND idempotency (same pos re-close dedupes, never double-counts).
            "order_id": f"spread_close_{pos_id}",
            "position_id": pos_id,
            "user_id": user_id,
            "strategy_id": strategy_id,
            "symbol": position.get("symbol"),
            "target_symbol": position.get("target_symbol"),
            "trading_symbol": position.get("target_symbol"),
            "underlying": position.get("underlying"),
            "asset_type": "option_spread",
            "structure": "credit_spread",
            "side": "BUY",  # buy-to-close the net credit spread
            "qty": qty,
            "price": close_value,
            "charges": round(entry_charges + exit_charges, 2),
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "realized_pnl": net_pnl,
            "mode": position.get("mode"),
            "action": "CLOSE",
            "exit_reason": reason,
            "created_at": closed_at,
        })
    except Exception as exc:  # audit row must never break the close/accounting
        logger.error("Spread close trade_fills insert failed for %s: %s", pos_id, exc)

    # strategy.today_pnl / total_pnl is a CACHE field maintained identically by
    # PortfolioLedger on every full close (each writer counts its own trades once,
    # so no double-count). The source of truth for P&L readers is db.trade_fills
    # above; this $inc only feeds the risk/throttle gates that read strategy.today_pnl.
    await db.strategies.update_one(
        {"id": strategy_id, "user_id": user_id},
        {"$set": {"last_pnl": net_pnl},
         "$inc": {"today_pnl": net_pnl, "total_pnl": net_pnl, "total_trades": 1,
                  "wins": 1 if net_pnl > 0 else 0, "losses": 1 if net_pnl < 0 else 0}},
    )

    logger.info(
        "Spread CLOSE %s reason=%s close_value=%.2f gross=%.2f net=%.2f",
        pos_id, reason, close_value, gross_pnl, net_pnl,
    )
    return {"ok": True, "status": "CLOSED", "id": pos_id, "realized_pnl": net_pnl,
            "gross_pnl": gross_pnl, "exit_value": close_value}


def value_debit_spread(position: Dict[str, Any], short_ltp: float, long_ltp: float) -> Dict[str, float]:
    """Current spread value (net credit to close, meaning sell the long and buy back the short) and unrealized P&L."""
    qty = int(position.get("open_quantity") or position.get("quantity") or 0)
    net_debit = float(position.get("net_debit") or position.get("average_buy_price") or 0.0)
    value = round(float(long_ltp) - float(short_ltp), 2)
    pnl = round((value - net_debit) * qty, 2)
    return {"value": value, "pnl": pnl, "net_debit": net_debit}


def debit_spread_exit_reason(position: Dict[str, Any], value: float) -> Optional[str]:
    """TP/SL on the spread value for debit spread. Returns reason or None."""
    tp_value = position.get("spread_tp_value")
    sl_value = position.get("spread_sl_value")
    if tp_value is not None and value >= float(tp_value):
        return "spread-tp"
    if sl_value is not None and value <= float(sl_value):
        return "spread-sl"
    return None


def compute_debit_exit_levels(net_debit: float, width: float) -> Dict[str, float]:
    tp_value = round(net_debit + (width - net_debit) * SPREAD_TP_FRAC, 2)
    sl_value = round(max(0.0, net_debit * (1.0 - DEBIT_SPREAD_SL_FRAC)), 2)
    return {"spread_tp_value": tp_value, "spread_sl_value": sl_value}


async def open_debit_spread(
    db,
    *,
    user_id: str,
    strategy_id: str,
    underlying: str,
    spread: Dict[str, Any],
    lots: int,
    lot_size: int,
    mode: str = "paper",
    idempotency_key: str,
    signal_id: Optional[str] = None,
    regime_at_entry: str = "UNKNOWN",
) -> Dict[str, Any]:
    """Open a debit spread as one position with embedded legs (paper only)."""
    if mode != "paper":
        return {"ok": False, "status": "SKIPPED", "reason": "debit spreads are paper-only for now"}
    if not spread.get("ok"):
        return {"ok": False, "status": "SKIPPED", "reason": spread.get("reason") or "invalid spread"}

    # Idempotency
    existing = await db.strategy_positions.find_one(
        {"user_id": user_id, "idempotency_key": idempotency_key,
         "status": {"$in": ["OPEN", "EXITING", "CLOSED"]}},
        {"_id": 0, "id": 1},
    )
    if existing:
        return {"ok": False, "status": "SKIPPED", "reason": "duplicate spread idempotency", "id": existing["id"]}

    qty = max(1, int(lots)) * max(1, int(lot_size))
    short = dict(spread["short_leg"])
    long = dict(spread["long_leg"])
    net_debit = float(spread["net_debit"])
    width = float(spread["width_points"])
    max_loss = float(spread["max_loss"])

    wallet = PaperWallet(db)
    pos_id = f"pos_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()

    short_charges = _leg_charges("SELL", short["premium"], qty)
    long_charges = _leg_charges("BUY", long["premium"], qty)
    long_cost = round(long["premium"] * qty + long_charges, 2)
    short_proceeds = round(short["premium"] * qty - short_charges, 2)

    # Debit wallet by long_cost, credit by short_proceeds
    funded = await wallet.debit(user_id, long_cost, f"{pos_id}:long:open")
    if not funded:
        return {"ok": False, "status": "SKIPPED", "reason": "insufficient paper funds for spread long leg"}
    await wallet.credit(user_id, short_proceeds, f"{pos_id}:short:open")

    entry_charges = round(short_charges + long_charges, 2)
    levels = compute_debit_exit_levels(net_debit, width)

    for leg, side in ((short, "SELL"), (long, "BUY")):
        leg["qty"] = qty
        leg["entry_price"] = leg["premium"]

    position_doc = {
        "id": pos_id,
        "user_id": user_id,
        "strategy_id": strategy_id,
        "symbol": underlying,
        "target_symbol": f"{underlying} {long.get('strike')}/{short.get('strike')} {spread.get('option_type')} DEBIT",
        "trading_symbol": f"{underlying}-{spread.get('direction')}-debit-spread",
        "mode": mode,
        "structure": "debit_spread",
        "direction": spread.get("direction"),
        "option_type": spread.get("option_type"),
        "underlying": underlying,
        "symbol_group": underlying,
        "expiry": long.get("expiry"),
        "asset_type": "option_spread",
        "asset_class": "OPTION_SPREAD",
        "position_side": "LONG",    # net long option premium
        "status": "OPEN",
        "legs": [short, long],
        "quantity": qty,
        "open_quantity": qty,
        "qty": qty,
        "lots": max(1, int(lots)),
        "lot_size": int(lot_size),
        "net_debit": round(net_debit, 2),
        "average_buy_price": round(net_debit, 2),  # entry "price" = net debit paid
        "average_price": round(net_debit, 2),
        "entry_price": round(net_debit, 2),
        "max_loss": round(max_loss, 2),
        "max_loss_total": round(max_loss * qty, 2),
        "spread_tp_value": levels["spread_tp_value"],
        "spread_sl_value": levels["spread_sl_value"],
        "net_delta": spread.get("net_delta"),
        "net_theta": spread.get("net_theta"),
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "pnl": 0.0,
        "entry_charges": entry_charges,
        "brokerage": entry_charges,
        "idempotency_key": idempotency_key,
        "signal_id": signal_id,
        "created_at": now,
        "updated_at": now,
        "entry_time": now,
        # HSI-13: exact attribution inputs stamped at creation.
        "regime_at_entry": str(regime_at_entry or "UNKNOWN").upper(),
        "planned_risk": round(max_loss * qty, 2),
    }
    await db.strategy_positions.insert_one(position_doc)

    for leg in (short, long):
        await db.orders.insert_one({
            "id": f"ord_{uuid.uuid4().hex[:12]}",
            "user_id": user_id,
            "strategy_id": strategy_id,
            "position_id": pos_id,
            "symbol": underlying,
            "target_symbol": leg.get("tradingsymbol") or f"{underlying} {leg.get('strike')} {leg.get('option_type')}",
            "instrument_key": leg.get("instrument_key"),
            "side": leg["side"],
            "qty": qty,
            "lot_size": int(lot_size),
            "filled_qty": qty,
            "price": leg["premium"],
            "requested_price": leg["premium"],
            "status": "FILLED",
            "execution_status": "FILLED",
            "mode": mode,
            "broker": "paper",
            "structure": "debit_spread",
            "spread_role": leg["role"],
            "idempotency_key": f"{idempotency_key}:{leg['role']}",
            "created_at": now,
            "updated_at": now,
        })

    logger.info(
        "Spread (debit) OPEN %s %s %s debit=%.2f max_loss=%.2f qty=%d tp_val=%.2f sl_val=%.2f",
        underlying, spread.get("direction"), spread.get("option_type"),
        net_debit, max_loss, qty, levels["spread_tp_value"], levels["spread_sl_value"],
    )
    return {"ok": True, "status": "FILLED", "id": pos_id, "position_id": pos_id,
            "net_debit": net_debit, "max_loss": max_loss}


async def close_debit_spread(
    db,
    position: Dict[str, Any],
    *,
    reason: str,
    short_ltp: float,
    long_ltp: float,
) -> Dict[str, Any]:
    """Close both legs of a debit spread atomically and realize P&L."""
    pos_id = position["id"]
    user_id = position["user_id"]
    strategy_id = position["strategy_id"]
    qty = int(position.get("open_quantity") or position.get("quantity") or 0)
    net_debit = float(position.get("net_debit") or position.get("average_buy_price") or 0.0)

    # Claim close
    now = datetime.now(timezone.utc).isoformat()
    claim = await db.strategy_positions.update_one(
        {"id": pos_id, "user_id": user_id, "status": {"$in": ["OPEN", "EXITING"]}},
        {"$set": {"status": "EXITING", "updated_at": now}},
    )
    if claim.modified_count == 0:
        return {"ok": False, "status": "SKIPPED", "reason": "spread already closing/closed"}

    short_ltp = float(short_ltp)
    long_ltp = float(long_ltp)
    close_value = round(long_ltp - short_ltp, 2)

    wallet = PaperWallet(db)
    buyback_charges = _leg_charges("BUY", short_ltp, qty)
    sell_charges = _leg_charges("SELL", long_ltp, qty)
    exit_charges = round(buyback_charges + sell_charges, 2)

    # Wallet updates
    await wallet.debit(user_id, round(short_ltp * qty + buyback_charges, 2), f"{pos_id}:short:close")
    await wallet.credit(user_id, max(0.0, round(long_ltp * qty - sell_charges, 2)), f"{pos_id}:long:close")

    gross_pnl = round((close_value - net_debit) * qty, 2)
    entry_charges = float(position.get("entry_charges") or 0.0)
    net_pnl = round(gross_pnl - exit_charges - entry_charges, 2)

    closed_at = datetime.now(timezone.utc).isoformat()
    await db.strategy_positions.update_one(
        {"id": pos_id, "user_id": user_id},
        {"$set": {
            "status": "CLOSED",
            "closed_at": closed_at,
            "updated_at": closed_at,
            "open_quantity": 0,
            "exit_reason": reason,
            "exit_value": close_value,
            "realized_pnl": net_pnl,
            "pnl": net_pnl,
            "gross_pnl": gross_pnl,
            "unrealized_pnl": 0.0,
        }},
    )

    await db.trades.insert_one({
        "id": f"trade_{uuid.uuid4().hex[:12]}",
        "position_id": pos_id,
        "user_id": user_id,
        "strategy_id": strategy_id,
        "symbol": position.get("symbol"),
        "target_symbol": position.get("target_symbol"),
        "underlying": position.get("underlying"),
        "asset_type": "option_spread",
        "structure": "debit_spread",
        "mode": position.get("mode"),
        "position_side": "LONG",
        "quantity": qty,
        "qty": qty,
        "entry_price": net_debit,
        "exit_price": close_value,
        "gross_pnl": gross_pnl,
        "charges": round(entry_charges + exit_charges, 2),
        "net_pnl": net_pnl,
        "pnl": net_pnl,
        "realized_pnl": net_pnl,
        "is_win": net_pnl > 0,
        "exit_reason": reason,
        "entry_time": position.get("entry_time"),
        "exit_time": closed_at,
        "created_at": closed_at,
    })

    try:
        await db.trade_fills.insert_one({
            "id": f"tf_spread_{pos_id}",
            "fill_id": f"tf_spread_{pos_id}",
            # Deterministic unique order_id: trade_fills has a unique (non-sparse)
            # index on order_id, so every spread close omitting it stored null and
            # the 2nd+ close collided (E11000). Per-position id keeps cross-spread
            # uniqueness AND idempotency (same pos re-close dedupes, never double-counts).
            "order_id": f"spread_close_{pos_id}",
            "position_id": pos_id,
            "user_id": user_id,
            "strategy_id": strategy_id,
            "symbol": position.get("symbol"),
            "target_symbol": position.get("target_symbol"),
            "trading_symbol": position.get("target_symbol"),
            "underlying": position.get("underlying"),
            "asset_type": "option_spread",
            "structure": "debit_spread",
            "side": "SELL",  # sell-to-close the net debit spread
            "qty": qty,
            "price": close_value,
            "charges": round(entry_charges + exit_charges, 2),
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "realized_pnl": net_pnl,
            "mode": position.get("mode"),
            "action": "CLOSE",
            "exit_reason": reason,
            "created_at": closed_at,
        })
    except Exception as exc:
        logger.error("Spread close trade_fills insert failed for %s: %s", pos_id, exc)

    await db.strategies.update_one(
        {"id": strategy_id, "user_id": user_id},
        {"$set": {"last_pnl": net_pnl},
         "$inc": {"today_pnl": net_pnl, "total_pnl": net_pnl, "total_trades": 1,
                  "wins": 1 if net_pnl > 0 else 0, "losses": 1 if net_pnl < 0 else 0}},
    )

    logger.info(
        "Spread (debit) CLOSE %s reason=%s close_value=%.2f gross=%.2f net=%.2f",
        pos_id, reason, close_value, gross_pnl, net_pnl,
    )
    return {"ok": True, "status": "CLOSED", "id": pos_id, "realized_pnl": net_pnl,
            "gross_pnl": gross_pnl, "exit_value": close_value}
