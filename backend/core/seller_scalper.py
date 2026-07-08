"""RES-7 — the regime-conditioned seller scalper (the strategy brain).

This is the strategy the whole roadmap builds toward. It does NOT re-implement
signals, sizing or exits — it ASSEMBLES the reusable machinery into one decision:

  RES-2  market_context   → is premium rich? is it a sell-safe regime? which side?
  RES-5  side_selector    → concrete spread (PE=bull-put / CE=bear-call)
  RES-4  reentry          → cadence: no pyramiding, per-day cap, post-exit cooldown
  RES-6  portfolio_risk   → book-level heat cap + correlation + daily-loss stop
  (RES-3 dynamic_exit already governs the EXIT in position_monitor: trailing lock)

`decide(context, book_state, config)` returns {enter, side, direction, structure,
reason, checks} — a single deterministic verdict. PURE, so the live runner and
the OOS backtester (RES-8) drive it identically. Nothing here trades or touches
a DB; the caller executes the plan and the OOS judge scores it.

Geometry defaults come from the only edge that survived OOS: the intraday
near-ATM width-1 credit-spread seller (thin, slippage-fragile — forward-paper,
never scale on backtest). All values are env-tunable and per-strategy overridable.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from core.side_selector import select_sell_spread
from core.reentry import evaluate_reentry, REENTRY_COOLDOWN_SECONDS, SCALP_DAILY_TRADE_CAP
from core.portfolio_risk import (
    evaluate_portfolio_gate, PORTFOLIO_HEAT_BUDGET,
    MAX_CORRELATED_PER_UNDERLYING, DAILY_LOSS_LIMIT,
)

# Geometry of the near-ATM width-1 seller (the OOS-surviving edge shape).
DEFAULT_CONFIG: Dict[str, Any] = {
    "underlying": "NIFTY",
    "structure": "credit_spread",
    "short_otm_steps": int(os.environ.get("SCALPER_SHORT_OTM_STEPS", "1")),   # sell 1 strike OTM
    "width_steps": int(os.environ.get("SCALPER_WIDTH_STEPS", "1")),           # buy 1 strike further
    "credit_tp_frac": float(os.environ.get("SCALPER_TP_FRAC", "0.35")),       # bank 35% of credit
    "credit_sl_mult": float(os.environ.get("SCALPER_SL_MULT", "1.5")),        # stop at 1.5x credit
    "daily_cap": SCALP_DAILY_TRADE_CAP,
    "cooldown_s": REENTRY_COOLDOWN_SECONDS,
    "heat_budget": PORTFOLIO_HEAT_BUDGET,
    "max_correlated": MAX_CORRELATED_PER_UNDERLYING,
    "daily_loss_limit": DAILY_LOSS_LIMIT,
    # entry window (IST minutes-of-day); sellers avoid the open and the last hour.
    "entry_start_min": int(os.environ.get("SCALPER_ENTRY_START_MIN", str(9 * 60 + 45))),
    "entry_end_min": int(os.environ.get("SCALPER_ENTRY_END_MIN", str(14 * 60 + 30))),
}


def default_config(**overrides: Any) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg


def _no(reason: str, stage: str, **extra: Any) -> Dict[str, Any]:
    return {"enter": False, "side": None, "direction": None, "structure": None,
            "reason": reason, "blocked_stage": stage, **extra}


def _within_window(minute_of_day: Optional[int], cfg: Dict[str, Any]) -> bool:
    if minute_of_day is None:
        return True  # caller didn't supply time → don't gate on it
    return cfg["entry_start_min"] <= int(minute_of_day) <= cfg["entry_end_min"]


def decide(
    *,
    context: Dict[str, Any],
    book_state: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble one entry decision. `book_state` (supplied live by the runner /
    historically by the backtester) carries:
        strategy_open_count      int   — open positions for THIS strategy (pyramiding)
        trades_today             int   — this strategy's trade count today (cap)
        seconds_since_last_exit  float|None — anti-churn cooldown
        open_positions           list  — ALL open positions (book heat + correlation)
        new_spread_max_loss      float — defined risk of the spread we'd open
        realized_today           float — signed book P&L today (daily-loss stop)
        minute_of_day            int|None — IST minute for the entry window
    """
    cfg = default_config(**(config or {}))

    # 0) entry window
    if not _within_window(book_state.get("minute_of_day"), cfg):
        return _no(f"outside entry window "
                   f"{cfg['entry_start_min']}–{cfg['entry_end_min']} IST-min", "window")

    # 1) RES-2 gate + RES-5 side: is premium rich, regime sell-safe, which side?
    sel = select_sell_spread(context)
    if not sel["ok"]:
        return _no(sel["reason"], "signal")
    side, direction = sel["side"], sel["direction"]
    new_bias = "BULLISH" if direction == "bullish" else "BEARISH"

    # 2) RES-4 cadence: no pyramiding, per-day cap, post-exit cooldown
    re = evaluate_reentry(
        has_open_position=int(book_state.get("strategy_open_count", 0)) > 0,
        trades_today=int(book_state.get("trades_today", 0)),
        daily_cap=cfg["daily_cap"],
        seconds_since_last_exit=book_state.get("seconds_since_last_exit"),
        cooldown_s=cfg["cooldown_s"],
    )
    if not re["allow"]:
        return _no(re["reason"], "cadence")

    # 3) RES-6 book-level risk: heat + correlation + daily-loss
    pg = evaluate_portfolio_gate(
        book_state.get("open_positions") or [],
        new_risk=float(book_state.get("new_spread_max_loss", 0.0)),
        underlying=cfg["underlying"],
        new_bias=new_bias,
        realized_today=float(book_state.get("realized_today", 0.0)),
        heat_budget=cfg["heat_budget"],
        max_same=cfg["max_correlated"],
        daily_loss_limit=cfg["daily_loss_limit"],
    )
    if not pg["allow"]:
        return _no(pg["reason"], "portfolio", checks={"portfolio": pg})

    return {
        "enter": True,
        "side": side,                    # PE / CE
        "direction": direction,          # bullish / bearish
        "structure": cfg["structure"],   # credit_spread
        "short_otm_steps": cfg["short_otm_steps"],
        "width_steps": cfg["width_steps"],
        "credit_tp_frac": cfg["credit_tp_frac"],
        "credit_sl_mult": cfg["credit_sl_mult"],
        "reason": sel["reason"],
        "blocked_stage": None,
        "checks": {"portfolio": pg},
    }
