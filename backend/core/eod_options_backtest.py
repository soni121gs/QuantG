"""EOD option backtester + walk-forward / OOS validator.

Prices the live option structures (single_leg / credit_spread / debit_spread) on
REAL settlement premiums from the free NSE/BSE bhavcopy store (core/bhavcopy_store),
day by day, over years of history. This is the engine that finally answers "does
this strategy have an edge?" for the slow, held-to-theta book — the profitable
cluster per attribution.

Granularity is DAILY (settle-to-settle). It faithfully models theta decay across
days, expiry settlement, brokerage and a conservative slippage on every leg. It
does NOT model intraday entries/exits — so it validates swing / held-to-theta
strategies, not scalpers (which need paid 1-min data and are the losing cluster
anyway).

The walk-forward split reports per-YEAR and per-MONTH results and only calls
something an edge when it is positive OUT-OF-SAMPLE (a later year the config was
never chosen on) AND consistent across months — not one lucky stretch.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from safe_exec import safe_run_strategy
from core.metrics import compute_metrics, grade
from core.bhavcopy_store import BhavcopyStore

logger = logging.getLogger("quantg.eod_options_backtest")

LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 65,
             "MIDCPNIFTY": 120, "BANKEX": 30, "NIFTYNXT50": 120}
BROKERAGE_PER_LEG = 20.0
SLIPPAGE_PCT = float(os.environ.get("BACKTEST_SLIPPAGE_PCT", "0.03"))  # adverse, per leg, each side
MIN_DTE_DAYS = 2   # avoid 0-DTE gamma; give theta room
MAX_DTE_DAYS = 10  # prefer the near weekly


def _dte(entry_day: str, expiry: str) -> int:
    try:
        a = datetime.strptime(entry_day, "%Y-%m-%d")
        b = datetime.strptime(expiry, "%Y-%m-%d")
        return (b - a).days
    except Exception:  # noqa: BLE001
        return 9999


def _pick_expiry(store: BhavcopyStore, u: str, day: str) -> Optional[str]:
    exps = store.expiries(u, day)
    weeklies = [e for e in exps if MIN_DTE_DAYS <= _dte(day, e) <= MAX_DTE_DAYS]
    if weeklies:
        return min(weeklies, key=lambda e: _dte(day, e))
    future = [e for e in exps if _dte(day, e) >= MIN_DTE_DAYS]
    return min(future, key=lambda e: _dte(day, e)) if future else None


def _strike_interval(strikes: List[float]) -> float:
    vals = sorted(set(strikes))
    diffs = [b - a for a, b in zip(vals, vals[1:]) if b > a]
    return min(diffs) if diffs else 50.0


def _fill(px: float, side: str) -> float:
    """Adverse slippage: a BUY pays up, a SELL receives less."""
    return px * (1 + SLIPPAGE_PCT) if side == "BUY" else px * (1 - SLIPPAGE_PCT)


class EODOptionsBacktest:
    def __init__(self, store: Optional[BhavcopyStore] = None):
        self.store = store or BhavcopyStore()

    def run(self, strategy: Dict[str, Any], start: Optional[str] = None,
            end: Optional[str] = None, starting_capital: float = 100_000.0) -> Dict[str, Any]:
        vc = strategy.get("visual_config") or {}
        opt = vc.get("options") or {}
        risk = vc.get("risk") or {}
        u = (opt.get("underlying") or vc.get("symbol") or "NIFTY").upper()
        structure = opt.get("structure") or "single_leg"
        lots = int(opt.get("lots") or 1)
        width = int(opt.get("spread_width") or 2)
        tp_pct = float(risk.get("target_pct") or risk.get("take_profit_pct") or 11) / 100.0
        sl_pct = float(risk.get("stoploss_pct") or risk.get("stop_loss_pct") or 7) / 100.0
        max_hold_days = int(risk.get("max_hold_days") or MAX_DTE_DAYS)

        candles = self.store.underlying_daily(u, start, end)
        if len(candles) < 40:
            return {"error": f"insufficient bhavcopy history for {u} ({len(candles)} days)",
                    "underlying": u, "structure": structure}

        code = strategy.get("python_code")
        if not code:
            return {"error": "strategy has no python_code", "underlying": u, "structure": structure}
        try:
            signals = safe_run_strategy(code, candles)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"strategy code failed: {exc}", "underlying": u, "structure": structure}
        sig_days = {str(s.get("date"))[:10]: s for s in (signals or [])}

        days = [c["date"][:10] for c in candles]
        close_by_day = {c["date"][:10]: c["close"] for c in candles}
        trades: List[Dict[str, Any]] = []
        active: Optional[Dict[str, Any]] = None

        for i, day in enumerate(days):
            # ---- manage open position ----
            if active:
                val = self._value(active, u, day)
                reason = None
                if val is not None:
                    basis, ref = active["entry_basis"], active["entry_ref"]
                    if active["kind"] == "credit":
                        captured = (basis - val) / basis if basis else 0.0  # % of credit kept
                        if captured >= active["tp"]:
                            reason = "TAKE_PROFIT"
                        elif (val - basis) >= active["sl"] * basis:
                            reason = "STOP_LOSS"
                    else:  # long premium (single_leg / debit)
                        rel = (val - basis) / ref if ref else 0.0
                        if rel >= active["tp"]:
                            reason = "TAKE_PROFIT"
                        elif rel <= -active["sl"]:
                            reason = "STOP_LOSS"
                    held = i - active["entry_idx"]
                    if day >= active["expiry"]:
                        reason = reason or "EXPIRY"
                    elif held >= max_hold_days:
                        reason = reason or "TIME_EXIT"
                if reason and val is not None:
                    self._close(active, day, val, reason, trades)
                    active = None

            # ---- look for entry ----
            if active is None and day in sig_days and i < len(days) - 1:
                pos = self._open(sig_days[day], u, day, i, close_by_day[day],
                                 structure, width, tp_pct, sl_pct)
                if pos:
                    ref_k = pos.get("short_k") or pos["legs"][0][1]
                    ref_typ = pos.get("typ") or pos["legs"][0][2]
                    real_lot = self.store.leg_lot_size(u, day, pos["expiry"], ref_k, ref_typ)
                    pos["lot"] = (real_lot or LOT_SIZES.get(u, 50)) * lots
                    active = pos

        if active:  # close hanging at last day
            val = self._value(active, u, days[-1])
            if val is not None:
                self._close(active, days[-1], val, "FORCE_CLOSE_END", trades)

        pnls = [t["pnl"] for t in trades]
        metrics = compute_metrics(pnls, starting_capital=starting_capital)
        return {
            "strategy_id": strategy.get("id"), "name": strategy.get("name"),
            "underlying": u, "structure": structure, "lots": lots,
            "window": {"start": days[0], "end": days[-1], "n_days": len(days)},
            "signals": len(sig_days), "grade": grade(metrics),
            **metrics, "trades": trades,
        }

    # ---- structure open / value / close --------------------------------------
    def _open(self, sig, u, day, idx, spot, structure, width, tp_pct, sl_pct):
        expiry = _pick_expiry(self.store, u, day)
        if not expiry:
            return None
        chain = self.store.option_chain(u, day).get(expiry, {})
        if not chain:
            return None
        strikes = list(chain.keys())
        interval = _strike_interval(strikes)
        atm = min(strikes, key=lambda k: abs(k - spot))
        action = (sig.get("action") or "").upper()
        direction = (sig.get("direction") or "").upper()
        bullish = action == "BUY" or direction == "CE"

        def settle(strike, typ):
            return self.store.leg_settle(u, day, expiry, strike, typ)

        if structure == "single_leg":
            typ = direction if direction in ("CE", "PE") else ("CE" if bullish else "PE")
            px = settle(atm, typ)
            if not px:
                return None
            entry = _fill(px, "BUY")
            return {"kind": "long", "structure": structure, "u": u, "expiry": expiry,
                    "legs": [("BUY", atm, typ)], "entry_basis": entry, "entry_ref": entry,
                    "tp": tp_pct, "sl": sl_pct, "entry_idx": idx, "entry_day": day,
                    "desc": f"BUY {atm:.0f}{typ} @{entry:.1f}"}

        if structure == "credit_spread":
            typ = "PE" if bullish else "CE"
            short_k = atm
            long_k = atm - width * interval if bullish else atm + width * interval
            sp, lp = settle(short_k, typ), settle(long_k, typ)
            if not sp or lp is None:
                return None
            credit = _fill(sp, "SELL") - _fill(lp, "BUY")
            if credit <= 0:
                return None
            return {"kind": "credit", "structure": structure, "u": u, "expiry": expiry,
                    "short_k": short_k, "long_k": long_k, "typ": typ,
                    "entry_basis": credit, "entry_ref": credit, "tp": 0.5, "sl": 1.0,
                    "entry_idx": idx, "entry_day": day,
                    "desc": f"SELL {short_k:.0f}/{long_k:.0f}{typ} credit={credit:.1f}"}

        if structure == "debit_spread":
            typ = "CE" if bullish else "PE"
            long_k = atm
            short_k = atm + width * interval if bullish else atm - width * interval
            lp, sp = settle(long_k, typ), settle(short_k, typ)
            if not lp or sp is None:
                return None
            debit = _fill(lp, "BUY") - _fill(sp, "SELL")
            if debit <= 0:
                return None
            return {"kind": "long", "structure": structure, "u": u, "expiry": expiry,
                    "short_k": short_k, "long_k": long_k, "typ": typ,
                    "entry_basis": debit, "entry_ref": debit, "tp": 0.5, "sl": 0.5,
                    "entry_idx": idx, "entry_day": day,
                    "desc": f"BUY {long_k:.0f}/{short_k:.0f}{typ} debit={debit:.1f}"}
        return None

    def _value(self, pos, u, day):
        s = pos["structure"]
        if s == "single_leg":
            _, k, typ = pos["legs"][0]
            px = self.store.leg_settle(u, day, pos["expiry"], k, typ)
            return _fill(px, "SELL") if px else None
        # spreads: value = cost to close (credit) / spread value (debit)
        short_px = self.store.leg_settle(u, day, pos["expiry"], pos["short_k"], pos["typ"])
        long_px = self.store.leg_settle(u, day, pos["expiry"], pos["long_k"], pos["typ"])
        if short_px is None or long_px is None:
            return None
        if s == "credit_spread":
            return _fill(short_px, "BUY") - _fill(long_px, "SELL")   # buy back short, sell long
        return _fill(long_px, "SELL") - _fill(short_px, "BUY")       # debit: sell long, buy back short

    def _close(self, pos, day, val, reason, trades):
        s = pos["structure"]
        if s == "credit_spread":
            pnl_per_unit = pos["entry_basis"] - val
        else:
            pnl_per_unit = val - pos["entry_basis"]
        legs = 1 if s == "single_leg" else 2
        gross = pnl_per_unit * pos["lot"]
        costs = BROKERAGE_PER_LEG * legs * 2
        trades.append({
            "structure": s, "desc": pos["desc"], "entry_date": pos["entry_day"], "exit_date": day,
            "exit_reason": reason, "entry_basis": round(pos["entry_basis"], 2),
            "exit_basis": round(val, 2), "gross_pnl": round(gross, 2), "pnl": round(gross - costs, 2),
        })


# ---- walk-forward / OOS verdict --------------------------------------------

def _bucket_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnls = [t["pnl"] for t in trades]
    n = len(pnls)
    if not n:
        return {"n": 0, "pnl": 0.0, "expectancy": 0.0, "win_rate": 0.0}
    wins = [p for p in pnls if p > 0]
    return {"n": n, "pnl": round(sum(pnls), 0),
            "expectancy": round(sum(pnls) / n, 1),
            "win_rate": round(100 * len(wins) / n, 1)}


def walk_forward(result: Dict[str, Any]) -> Dict[str, Any]:
    """Split a full-window backtest into per-year (train/OOS) and per-month buckets
    and emit an honest verdict. No parameter fitting — the OOS test is temporal:
    does the edge persist into later years/months it wasn't observed on?"""
    trades = result.get("trades") or []
    by_year: Dict[str, List] = {}
    by_month: Dict[str, List] = {}
    for t in trades:
        y = t["exit_date"][:4]
        m = t["exit_date"][:7]
        by_year.setdefault(y, []).append(t)
        by_month.setdefault(m, []).append(t)

    years = {y: _bucket_metrics(ts) for y, ts in sorted(by_year.items())}
    months = {m: _bucket_metrics(ts) for m, ts in sorted(by_month.items())}
    overall = _bucket_metrics(trades)

    year_keys = sorted(years.keys())
    oos_year = year_keys[-1] if year_keys else None            # latest = out-of-sample
    green_months = sum(1 for b in months.values() if b["pnl"] > 0)
    pct_green = round(100 * green_months / len(months), 1) if months else 0.0
    all_years_positive = bool(year_keys) and all(years[y]["pnl"] > 0 for y in year_keys)
    oos_positive = bool(oos_year) and years[oos_year]["expectancy"] > 0

    # verdict
    if overall["n"] < 30:
        verdict = "INSUFFICIENT_DATA"
    elif overall["expectancy"] <= 0:
        verdict = "NO_EDGE_NEGATIVE"
    elif all_years_positive and oos_positive and pct_green >= 55:
        verdict = "CANDIDATE_EDGE"
    else:
        verdict = "FRAGILE"

    return {
        "verdict": verdict, "overall": overall,
        "oos_year": oos_year, "oos": years.get(oos_year, {}),
        "by_year": years, "pct_green_months": pct_green,
        "n_months": len(months), "green_months": green_months,
        "all_years_positive": all_years_positive,
    }
