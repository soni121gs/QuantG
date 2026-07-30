"""IMD-07: intraday options backtest engine.

Replays a strategy minute-by-minute over ONE trading day: evaluate the signal on
the underlying minutes, select the option/spread with no lookahead (IMD-06), then
track the position on the selected contract's own minute candles with realistic
exits (stop / target / trailing / max-hold / 15:29 squareoff / missing-price) and
costs (slippage + brokerage). Deterministic: when several exit events fall inside
one candle the priority is fixed STOP -> TARGET -> TRAILING -> TIME/SQUAREOFF, and
a missing option price fails closed (exit at last-known mark, flagged) rather than
inventing a fill.

Pure logic with injected providers (underlying minutes, signal fn, chain-at-time,
option-series) so it unit-tests with tiny fixtures and no store/broker. IMD-08
drives it over many days for the walk-forward OOS verdict.

Buyer semantics: the position is LONG the net premium (single_leg = the option;
debit_spread = buy near - sell wing). R (risk unit) = entry net premium. Exit
R-levels: target at +target_R*R, initial stop at -initial_stop_R*R, trailing stop
arms once +trail_after_R*R is seen and then follows the peak down by initial_stop_R*R.
"""
from __future__ import annotations

import os

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from core.intraday_option_selector import select_contract

# exit reasons
EXIT_STOP = "stop"
EXIT_TARGET = "target"
EXIT_TRAIL = "trailing-stop"
EXIT_TIME = "time-exit"
EXIT_SQUAREOFF = "squareoff-1525"
EXIT_MISSING = "missing-price"


@dataclass
class IntradayCosts:
    # 2026-07-30: was 0.02 (2% per side). Measured real half-spread is 0.12-0.16%
    # per leg over 4,587 stored quotes, so 2% was ~14x reality and every verdict
    # this judge produced was computed at that cost. 0.005 keeps a ~4x buffer.
    slippage_pct: float = float(os.environ.get("INTRADAY_SLIPPAGE_PCT", "0.005"))
    brokerage_per_leg: float = 20.0  # flat, per leg, per side


@dataclass
class Trade:
    underlying: str
    date: str
    setup_type: str
    direction: str
    structure: str
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    lot_size: int
    lots: int
    gross_pnl: float
    net_pnl: float
    r_multiple: float
    mfe: float
    mae: float
    exit_reason: str
    data_quality_flags: List[str] = field(default_factory=list)


def _minute(ts: str) -> str:
    return str(ts)[:16]


def _index_series(candles: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {_minute(c["timestamp_ist"]): c for c in candles}


def run_day(
    *,
    underlying: str,
    date: str,
    underlying_minutes: List[Dict[str, Any]],
    signal_fn: Callable[[List[Dict[str, Any]]], Optional[Dict[str, Any]]],
    chain_at: Callable[[str], Dict[str, Any]],
    option_series: Dict[str, List[Dict[str, Any]]],
    costs: Optional[IntradayCosts] = None,
    max_trades: int = 1,
    lots: int = 1,
) -> List[Trade]:
    costs = costs or IntradayCosts()
    series = {k: _index_series(v) for k, v in option_series.items()}
    trades: List[Trade] = []
    i = 0
    n = len(underlying_minutes)

    while i < n and len(trades) < max_trades:
        history = underlying_minutes[: i + 1]
        bar = underlying_minutes[i]
        sig = signal_fn(history)
        if not sig:
            i += 1
            continue

        ts = bar["timestamp_ist"]
        sel = select_contract(
            underlying=underlying, spot=float(bar["close"]), chain=chain_at(ts),
            direction=str(sig.get("direction", "CE")),
            structure=str(sig.get("structure", "single_leg")),
            otm_offset_strikes=int(sig.get("otm_offset_strikes", 0)),
            spread_width=int(sig.get("spread_width", 2)),
        )
        if not sel.ok:
            i += 1
            continue

        def net_at(minute_key: str) -> Optional[Dict[str, float]]:
            hi = lo = cl = 0.0
            ok = False
            for leg in sel.legs:
                c = series.get(leg.expired_instrument_key, {}).get(minute_key)
                if c is None:
                    return None
                ok = True
                sign = 1.0 if leg.side == "BUY" else -1.0
                cl += sign * float(c["close"])
                if leg.side == "BUY":
                    hi += float(c.get("high", c["close"]))
                    lo += float(c.get("low", c["close"]))
                else:  # short leg: its high hurts the net, low helps
                    hi -= float(c.get("low", c["close"]))
                    lo -= float(c.get("high", c["close"]))
            if not ok:
                return None
            return {"high": hi, "low": lo, "close": cl}

        entry_key = _minute(ts)
        entry_mark = net_at(entry_key)
        if entry_mark is None:
            i += 1  # fail closed — no price, no trade
            continue

        entry_net = entry_mark["close"]
        # Long premium (single_leg / debit_spread) has a POSITIVE net (a debit
        # paid); a credit_spread has a NEGATIVE net (premium received). Reject the
        # wrong sign — fail closed, never trade a mispriced entry.
        is_credit = str(sig.get("structure", "")) == "credit_spread"
        if (is_credit and entry_net >= 0) or (not is_credit and entry_net <= 0):
            i += 1
            continue

        max_hold = int(sig.get("max_hold_minutes", 999))
        trail_R = float(sig.get("trail_after_R", 1e9))
        if is_credit:
            # Short premium: profit as the net rises toward 0 (spread decays). Book
            # tp_frac of the credit; stop at sl_mult x the credit; trail the peak.
            credit = -entry_net
            tp_frac = float(sig.get("credit_tp_frac", 0.35))
            sl_mult = float(sig.get("credit_sl_mult", 1.5))
            stop_R = float(sig.get("trail_giveback_R", 0.5))
            R = credit
            target_level = entry_net + tp_frac * credit
            stop_level = entry_net - sl_mult * credit
            trail_arm_level = entry_net + trail_R * credit
        else:
            R = entry_net
            target_R = float(sig.get("target_R", 1.4))
            stop_R = float(sig.get("initial_stop_R", 0.7))
            target_level = entry_net * (1 + target_R)
            stop_level = entry_net * (1 - stop_R)
            trail_arm_level = entry_net * (1 + trail_R)

        peak = entry_net
        trailing_armed = False
        mfe = mae = 0.0
        flags: List[str] = []
        exit_net = entry_net
        exit_reason = EXIT_SQUAREOFF
        exit_ts = ts
        last_mark = entry_net

        j = i + 1
        held = 0
        while j < n:
            held += 1
            key = _minute(underlying_minutes[j]["timestamp_ist"])
            mark = net_at(key)
            cur_ts = underlying_minutes[j]["timestamp_ist"]
            if mark is None:
                flags.append(f"missing_price@{key}")
                if held >= max_hold or j == n - 1:
                    exit_net, exit_reason, exit_ts = last_mark, EXIT_MISSING, cur_ts
                    break
                j += 1
                continue

            last_mark = mark["close"]
            mfe = max(mfe, mark["high"] - entry_net)
            mae = min(mae, mark["low"] - entry_net)
            peak = max(peak, mark["high"])
            if not trailing_armed and mark["high"] >= trail_arm_level:
                trailing_armed = True

            # deterministic priority within the bar
            if mark["low"] <= stop_level:
                exit_net, exit_reason, exit_ts = stop_level, EXIT_STOP, cur_ts
                break
            if trailing_armed:
                trail_level = peak - stop_R * R
                if mark["low"] <= trail_level:
                    exit_net, exit_reason, exit_ts = trail_level, EXIT_TRAIL, cur_ts
                    break
            if mark["high"] >= target_level:
                exit_net, exit_reason, exit_ts = target_level, EXIT_TARGET, cur_ts
                break
            if held >= max_hold:
                exit_net, exit_reason, exit_ts = mark["close"], EXIT_TIME, cur_ts
                break
            if j == n - 1:
                exit_net, exit_reason, exit_ts = mark["close"], EXIT_SQUAREOFF, cur_ts
                break
            j += 1

        lot = sel.lot_size
        qty = lot * lots
        gross = (exit_net - entry_net) * qty
        # Adverse slippage always REDUCES P&L regardless of sign (long or short):
        # charge it on the magnitude of both fills, so a credit spread (negative
        # net) isn't rewarded by the old positive-premium-only fill formula.
        brokerage = costs.brokerage_per_leg * len(sel.legs) * 2
        slippage_cost = costs.slippage_pct * (abs(entry_net) + abs(exit_net)) * qty
        net = gross - slippage_cost - brokerage
        trades.append(Trade(
            underlying=underlying, date=date, setup_type=str(sig.get("setup_type", "")),
            direction=str(sig.get("direction", "")), structure=sel.structure,
            entry_ts=ts, exit_ts=exit_ts, entry_price=round(entry_net, 4),
            exit_price=round(exit_net, 4), lot_size=lot, lots=lots,
            gross_pnl=round(gross, 2), net_pnl=round(net, 2),
            r_multiple=round((exit_net - entry_net) / R, 4) if R else 0.0,
            mfe=round(mfe, 4), mae=round(mae, 4), exit_reason=exit_reason,
            data_quality_flags=flags,
        ))
        i = j + 1

    return trades
