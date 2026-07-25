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
import statistics
from datetime import datetime, timedelta
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
# P5-J1: minimum |t-stat| for a positive expectancy to count as a CANDIDATE_EDGE.
# 3.0 is the standard bar for a NEW signal (article + CLAUDE.md §20). Env-tunable.
T_STAT_MIN = float(os.environ.get("JUDGE_T_STAT_MIN", "3.0"))


def _dte(entry_day: str, expiry: str) -> int:
    try:
        a = datetime.strptime(entry_day, "%Y-%m-%d")
        b = datetime.strptime(expiry, "%Y-%m-%d")
        return (b - a).days
    except Exception:  # noqa: BLE001
        return 9999


def _pick_expiry(store: BhavcopyStore, u: str, day: str,
                 min_dte: int = MIN_DTE_DAYS, max_dte: int = MAX_DTE_DAYS) -> Optional[str]:
    exps = store.expiries(u, day)
    weeklies = [e for e in exps if min_dte <= _dte(day, e) <= max_dte]
    if weeklies:
        return min(weeklies, key=lambda e: _dte(day, e))
    future = [e for e in exps if _dte(day, e) >= min_dte]
    return min(future, key=lambda e: _dte(day, e)) if future else None


def _pick_calendar_expiries(store: BhavcopyStore, u: str, day: str,
                            near_min: int = MIN_DTE_DAYS, near_max: int = MAX_DTE_DAYS,
                            far_min: int = 21, far_max: int = 60) -> Optional[Tuple[str, str]]:
    exps = sorted(store.expiries(u, day), key=lambda e: _dte(day, e))
    near = next((e for e in exps if near_min <= _dte(day, e) <= near_max), None)
    if not near:
        return None
    far = next((e for e in exps if e > near and far_min <= _dte(day, e) <= far_max), None)
    if not far:
        far = next((e for e in exps if e > near), None)
    return (near, far) if far else None


def _strike_interval(strikes: List[float]) -> float:
    vals = sorted(set(strikes))
    diffs = [b - a for a, b in zip(vals, vals[1:]) if b > a]
    return min(diffs) if diffs else 50.0


def _fill(px: float, side: str) -> float:
    """Adverse slippage: a BUY pays up, a SELL receives less."""
    return px * (1 + SLIPPAGE_PCT) if side == "BUY" else px * (1 - SLIPPAGE_PCT)


def _signal_days_from(signals: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(s.get("date"))[:10]: s for s in (signals or []) if s.get("date")}


def _event_window_days(event_dates: List[str], width: int) -> set[str]:
    out: set[str] = set()
    for raw in event_dates or []:
        try:
            d0 = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except Exception:  # noqa: BLE001
            continue
        for offset in range(-abs(width), abs(width) + 1):
            out.add((d0 + timedelta(days=offset)).isoformat())
    return out


class EODOptionsBacktest:
    def __init__(self, store: Optional[BhavcopyStore] = None):
        self.store = store or BhavcopyStore()

    def run(self, strategy: Dict[str, Any], start: Optional[str] = None,
            end: Optional[str] = None, starting_capital: float = 100_000.0,
            params: Optional[Dict[str, Any]] = None,
            signals: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """`params` (optional) overrides config for edge-search sweeps:
        credit_tp / credit_sl (spread exit geometry), debit_tp / debit_sl,
        width (strikes), min_dte / max_dte (expiry choice), max_hold_days.
        calendar_spread uses near expiry min/max plus calendar_far_min_dte /
        calendar_far_max_dte for the long leg."""
        p = params or {}
        self._p = p
        vc = strategy.get("visual_config") or {}
        opt = vc.get("options") or {}
        risk = vc.get("risk") or {}
        u = (opt.get("underlying") or vc.get("symbol") or "NIFTY").upper()
        structure = opt.get("structure") or "single_leg"
        lots = int(opt.get("lots") or 1)
        width = int(p.get("width") or opt.get("spread_width") or 2)
        # iron_condor geometry: short legs `short_otm_pct` OTM, long wings `wing_width` strikes beyond
        _otm_raw = p.get("short_otm_pct")
        if _otm_raw is None:
            _otm_raw = opt.get("short_otm_pct")
        self._short_otm = float(_otm_raw) if _otm_raw is not None else 0.02   # condor default 2% OTM
        self._credit_otm = float(_otm_raw) if _otm_raw is not None else 0.0   # credit_spread: ATM unless set
        self._wing = int(p.get("wing_width") or opt.get("wing_width") or width or 4)
        # exit_mode="expiry" holds a defined-risk structure to expiry with NO intraday
        # tp/sl — required for condors, whose max loss is measured in wing-widths, not
        # credit-multiples, so any credit-based stop is meaningless and the illiquid
        # far wings make daily marks unreliable. Exits only on EXPIRY / TIME.
        self._exit_mode = (p.get("exit_mode") or opt.get("exit_mode") or "").lower()
        # delta-1 trend specialist: buy a DEEP-ITM single leg (low theta, ~delta 0.7+)
        # `itm_offset_pct` in the money so the option tracks the index move, not decay.
        _itm = p.get("itm_offset_pct")
        if _itm is None:
            _itm = opt.get("itm_offset_pct")
        self._itm_offset = float(_itm) if _itm is not None else 0.0
        tp_pct = float(risk.get("target_pct") or risk.get("take_profit_pct") or 11) / 100.0
        sl_pct = float(risk.get("stoploss_pct") or risk.get("stop_loss_pct") or 7) / 100.0
        max_hold_days = int(p.get("max_hold_days") or risk.get("max_hold_days") or MAX_DTE_DAYS)

        candles = self.store.underlying_daily(u, start, end)
        if len(candles) < 40:
            return {"error": f"insufficient bhavcopy history for {u} ({len(candles)} days)",
                    "underlying": u, "structure": structure}

        days = [c["date"][:10] for c in candles]
        close_by_day = {c["date"][:10]: c["close"] for c in candles}

        # RES-8: externally-generated signals (from a reconstructed historical
        # market_context) can be injected directly, bypassing python_code — the
        # same pricing/settlement engine then grades them identically.
        if signals is not None:
            sig_days = _signal_days_from(signals or [])
            signal_eval = "injected"
        else:
            code = strategy.get("python_code")
            if not code:
                return {"error": "strategy has no python_code", "underlying": u, "structure": structure}
            try:
                signals = safe_run_strategy(code, candles)
            except Exception as exc:  # noqa: BLE001
                return {"error": f"strategy code failed: {exc}", "underlying": u, "structure": structure}
            sig_days = _signal_days_from(signals or [])
            signal_eval = "whole_history"

            # Live strategies commonly evaluate only the latest candle (`data[-1]`).
            # A single whole-history call then emits only the final day's signal, which
            # cannot open/close historically. Replay prefixes to backtest that style.
            if len(sig_days) <= 1:
                rolling_sig_days: Dict[str, Dict[str, Any]] = {}
                for j in range(1, len(candles) - 1):
                    prefix = candles[: j + 1]
                    day = days[j]
                    try:
                        prefix_signals = safe_run_strategy(code, prefix) or []
                    except Exception as exc:  # noqa: BLE001
                        return {"error": f"strategy code failed during rolling evaluation on {day}: {exc}",
                                "underlying": u, "structure": structure}
                    today = [s for s in prefix_signals if str(s.get("date"))[:10] == day]
                    if today:
                        rolling_sig_days[day] = today[-1]
                if len(rolling_sig_days) > len(sig_days):
                    sig_days = rolling_sig_days
                    signal_eval = "rolling_latest_window"

        if p.get("event_dates"):
            window = int(p.get("event_window_days", 1))
            allowed = _event_window_days([str(d) for d in p.get("event_dates") or []], window)
            before = len(sig_days)
            sig_days = {d: s for d, s in sig_days.items() if d in allowed}
            signal_eval = f"{signal_eval}+event_window_{window}d"
            event_filter = {"enabled": True, "event_count": len(p.get("event_dates") or []),
                            "window_days": window, "signals_before": before,
                            "signals_after": len(sig_days)}
        else:
            event_filter = {"enabled": False}

        self._close_by = close_by_day   # index close per day, for cash-settled expiry payoff
        trades: List[Dict[str, Any]] = []
        active: Optional[Dict[str, Any]] = None

        for i, day in enumerate(days):
            # ---- manage open position ----
            if active:
                val = self._value(active, u, day)
                reason = None
                if val is not None:
                    basis, ref = active["entry_basis"], active["entry_ref"]
                    held = i - active["entry_idx"]
                    forced_hold = p.get("force_time_exit_days")
                    if forced_hold is not None and held >= int(forced_hold):
                        reason = "TIME_EXIT"
                    elif self._exit_mode != "expiry":
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
        from core.historical_regimes import aggregate_by_regime, tag_regimes
        regime_rows = tag_regimes(candles)
        regime_breakdown = aggregate_by_regime(
            trades, {row["date"]: row for row in regime_rows},
        )
        return {
            "strategy_id": strategy.get("id"), "name": strategy.get("name"),
            "underlying": u, "structure": structure, "lots": lots,
            "window": {"start": days[0], "end": days[-1], "n_days": len(days)},
            "signals": len(sig_days), "signal_evaluation": signal_eval, "grade": grade(metrics),
            "event_filter": event_filter, **metrics, "trades": trades, "regime_breakdown": regime_breakdown,
        }

    # ---- structure open / value / close --------------------------------------
    def _open(self, sig, u, day, idx, spot, structure, width, tp_pct, sl_pct):
        p = getattr(self, "_p", {}) or {}
        if structure == "calendar_spread":
            pair = _pick_calendar_expiries(
                self.store, u, day,
                int(p.get("min_dte") or MIN_DTE_DAYS),
                int(p.get("max_dte") or MAX_DTE_DAYS),
                int(p.get("calendar_far_min_dte") or 21),
                int(p.get("calendar_far_max_dte") or 60),
            )
            if not pair:
                return None
            expiry, far_expiry = pair
        else:
            expiry = _pick_expiry(self.store, u, day,
                                  int(p.get("min_dte") or MIN_DTE_DAYS),
                                  int(p.get("max_dte") or MAX_DTE_DAYS))
            far_expiry = None
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
            # deep-ITM (delta-1) selection when itm_offset_pct set: CE strike below
            # spot / PE strike above spot by that %, snapped to a listed strike.
            k = atm
            if self._itm_offset > 0:
                dist = max(1, round(spot * self._itm_offset / interval))
                target = atm - dist * interval if typ == "CE" else atm + dist * interval
                k = min(strikes, key=lambda s: abs(s - target))
            px = settle(k, typ)
            if not px:
                return None
            entry = _fill(px, "BUY")
            return {"kind": "long", "structure": structure, "u": u, "expiry": expiry,
                    "legs": [("BUY", k, typ)], "entry_basis": entry, "entry_ref": entry,
                    "tp": tp_pct, "sl": sl_pct, "entry_idx": idx, "entry_day": day,
                    "desc": f"BUY {k:.0f}{typ} @{entry:.1f}"}

        if structure == "credit_spread":
            typ = "PE" if bullish else "CE"
            # short leg `short_otm_pct` OTM (0 = ATM, the default). Put spread sits below
            # spot, call spread above; the long wing is `width` strikes further OTM.
            short_dist = round(spot * self._credit_otm / interval)
            if bullish:   # sell put spread (OTM below)
                short_k = atm - short_dist * interval
                long_k = short_k - width * interval
            else:         # sell call spread (OTM above)
                short_k = atm + short_dist * interval
                long_k = short_k + width * interval
            sp, lp = settle(short_k, typ), settle(long_k, typ)
            if not sp or lp is None:
                return None
            credit = _fill(sp, "SELL") - _fill(lp, "BUY")
            if credit <= 0:
                return None
            return {"kind": "credit", "structure": structure, "u": u, "expiry": expiry,
                    "short_k": short_k, "long_k": long_k, "typ": typ,
                    "entry_basis": credit, "entry_ref": credit,
                    "tp": float(p.get("credit_tp", 0.5)), "sl": float(p.get("credit_sl", 1.0)),
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

        if structure == "calendar_spread":
            typ = direction if direction in ("CE", "PE") else ("CE" if bullish else "PE")
            k = atm
            near_px = settle(k, typ)
            far_px = self.store.leg_settle(u, day, far_expiry, k, typ) if far_expiry else None
            if near_px is None or not far_px:
                return None
            debit = _fill(far_px, "BUY") - _fill(near_px, "SELL")
            if debit <= 0:
                return None
            return {"kind": "long", "structure": structure, "u": u, "expiry": expiry,
                    "far_expiry": far_expiry, "strike": k, "typ": typ,
                    "legs": [("SELL", k, typ, expiry), ("BUY", k, typ, far_expiry)],
                    "entry_basis": debit, "entry_ref": debit, "tp": float(p.get("debit_tp", 0.5)),
                    "sl": float(p.get("debit_sl", 0.5)), "entry_idx": idx, "entry_day": day,
                    "desc": f"CAL {k:.0f}{typ} short {expiry} long {far_expiry} debit={debit:.1f}"}

        if structure == "iron_condor":
            # Direction-agnostic, defined-risk short vol: sell an OTM strangle, buy
            # further-OTM wings to cap the tail. `short_otm_pct` sets the short legs;
            # `wing_width` strikes beyond sets the longs. Net credit; max loss capped
            # at (wing_width * interval - credit) per side.
            short_dist = max(1, round(spot * self._short_otm / interval))
            ce_s_k = atm + short_dist * interval
            pe_s_k = atm - short_dist * interval
            ce_l_k = ce_s_k + self._wing * interval
            pe_l_k = pe_s_k - self._wing * interval
            ce_s, ce_l = settle(ce_s_k, "CE"), settle(ce_l_k, "CE")
            pe_s, pe_l = settle(pe_s_k, "PE"), settle(pe_l_k, "PE")
            if not ce_s or not pe_s or ce_l is None or pe_l is None:
                return None
            credit = ((_fill(ce_s, "SELL") - _fill(ce_l, "BUY"))
                      + (_fill(pe_s, "SELL") - _fill(pe_l, "BUY")))
            if credit <= 0:
                return None
            return {"kind": "credit", "structure": structure, "u": u, "expiry": expiry,
                    "ce_short": ce_s_k, "ce_long": ce_l_k, "pe_short": pe_s_k, "pe_long": pe_l_k,
                    "short_k": ce_s_k, "typ": "CE",  # reference leg for lot sizing
                    "wing_pts": self._wing * interval,  # defined max cost-to-close (per side)
                    "entry_basis": credit, "entry_ref": credit,
                    "tp": float(p.get("credit_tp", 0.5)), "sl": float(p.get("credit_sl", 1.0)),
                    "entry_idx": idx, "entry_day": day,
                    "desc": f"IC {pe_l_k:.0f}/{pe_s_k:.0f}-{ce_s_k:.0f}/{ce_l_k:.0f} cr={credit:.1f}"}
        return None

    def _intrinsic_value(self, pos, s_exp):
        """Cost-to-close / mark at exact cash-settled index intrinsic — the faithful
        expiry mark (per-leg premiums are unreliable on expiry day)."""
        s = pos["structure"]
        if s == "single_leg":
            _, k, typ = pos["legs"][0]
            return max(0.0, s_exp - k) if typ == "CE" else max(0.0, k - s_exp)
        if s == "credit_spread":
            sk, lk, typ = pos["short_k"], pos["long_k"], pos["typ"]
            v = (max(0.0, sk - s_exp) - max(0.0, lk - s_exp)) if typ == "PE" \
                else (max(0.0, s_exp - sk) - max(0.0, s_exp - lk))
            return max(0.0, v)
        if s == "debit_spread":
            sk, lk, typ = pos["short_k"], pos["long_k"], pos["typ"]
            v = (max(0.0, s_exp - lk) - max(0.0, s_exp - sk)) if typ == "CE" \
                else (max(0.0, lk - s_exp) - max(0.0, sk - s_exp))
            return max(0.0, v)
        if s == "iron_condor":
            cs, cl, ps, pl = pos["ce_short"], pos["ce_long"], pos["pe_short"], pos["pe_long"]
            return max(0.0, (max(0.0, s_exp - cs) - max(0.0, s_exp - cl)
                             + max(0.0, ps - s_exp) - max(0.0, pl - s_exp)))
        if s == "calendar_spread":
            k, typ = pos["strike"], pos["typ"]
            return max(0.0, s_exp - k) if typ == "CE" else max(0.0, k - s_exp)
        return None

    def _value(self, pos, u, day):
        s = pos["structure"]
        if s == "calendar_spread":
            k, typ = pos["strike"], pos["typ"]
            far_px = self.store.leg_settle(u, day, pos["far_expiry"], k, typ)
            if far_px is None:
                return None
            if day >= pos["expiry"]:
                s_exp = getattr(self, "_close_by", {}).get(pos["expiry"]) or getattr(self, "_close_by", {}).get(day)
                if s_exp is None:
                    return None
                near_cost = max(0.0, s_exp - k) if typ == "CE" else max(0.0, k - s_exp)
            else:
                near_px = self.store.leg_settle(u, day, pos["expiry"], k, typ)
                if near_px is None:
                    return None
                near_cost = _fill(near_px, "BUY")
            return _fill(far_px, "SELL") - near_cost
        # Hold-to-expiry mode: settle at exact index intrinsic once at/after expiry.
        if getattr(self, "_exit_mode", "") == "expiry" and day >= pos["expiry"]:
            s_exp = getattr(self, "_close_by", {}).get(pos["expiry"]) or getattr(self, "_close_by", {}).get(day)
            if s_exp is not None:
                iv = self._intrinsic_value(pos, s_exp)
                if iv is not None:
                    return iv
        if s == "single_leg":
            _, k, typ = pos["legs"][0]
            px = self.store.leg_settle(u, day, pos["expiry"], k, typ)
            return _fill(px, "SELL") if px else None
        if s == "iron_condor":
            exp = pos["expiry"]
            cs, cl, ps, pl = pos["ce_short"], pos["ce_long"], pos["pe_short"], pos["pe_long"]
            # At/after expiry the options are CASH-settled at exact index intrinsic — use
            # the index close, NOT the illiquid/garbage per-leg premiums on expiry day
            # (which otherwise mark every trade at max loss). This mirrors the EDR-07 study.
            if day >= exp:
                s_exp = getattr(self, "_close_by", {}).get(exp) or getattr(self, "_close_by", {}).get(day)
                if s_exp is not None:
                    payoff = (max(0.0, s_exp - cs) - max(0.0, s_exp - cl)
                              + max(0.0, ps - s_exp) - max(0.0, pl - s_exp))
                    return max(0.0, payoff)
            # pre-expiry mark-to-market from leg premiums, capped at the defined wing width
            ce_s = self.store.leg_settle(u, day, exp, cs, "CE")
            ce_l = self.store.leg_settle(u, day, exp, cl, "CE")
            pe_s = self.store.leg_settle(u, day, exp, ps, "PE")
            pe_l = self.store.leg_settle(u, day, exp, pl, "PE")
            if None in (ce_s, ce_l, pe_s, pe_l):
                return None
            raw = ((_fill(ce_s, "BUY") - _fill(ce_l, "SELL"))
                   + (_fill(pe_s, "BUY") - _fill(pe_l, "SELL")))
            return max(0.0, min(raw, pos.get("wing_pts", raw)))
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
        if s in ("credit_spread", "iron_condor"):
            pnl_per_unit = pos["entry_basis"] - val   # credit kept minus cost to close
        else:
            pnl_per_unit = val - pos["entry_basis"]
        legs = {"single_leg": 1, "iron_condor": 4}.get(s, 2)
        gross = pnl_per_unit * pos["lot"]
        costs = BROKERAGE_PER_LEG * legs * 2
        trades.append({
            "structure": s, "desc": pos["desc"], "entry_date": pos["entry_day"], "exit_date": day,
            "exit_reason": reason, "entry_basis": round(pos["entry_basis"], 2),
            "exit_basis": round(val, 2), "gross_pnl": round(gross, 2), "pnl": round(gross - costs, 2),
        })


# ---- walk-forward / OOS verdict --------------------------------------------

def _bucket_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-bucket P&L stats INCLUDING dispersion (P5-J1, 2026-07-23).

    Until now this returned n / pnl / expectancy / win_rate and no second moment,
    so `+₹5/trade at σ=3000` and `+₹500/trade at σ=200` were indistinguishable —
    every verdict QuantG ever issued was made blind to variance. The t-statistic
    (`expectancy / standard_error`, H0: expectancy=0) is the honest test of whether
    a positive expectancy is signal or noise; a per-trade Sharpe is reported too.
    A single trade has no dispersion, so std/t_stat/sharpe are None then.
    """
    pnls = [float(t["pnl"]) for t in trades]
    n = len(pnls)
    if not n:
        return {"n": 0, "pnl": 0.0, "expectancy": 0.0, "win_rate": 0.0,
                "std": 0.0, "t_stat": None, "sharpe": None}
    wins = [p for p in pnls if p > 0]
    mean = sum(pnls) / n
    sd = statistics.stdev(pnls) if n > 1 else 0.0          # sample std (ddof=1)
    se = sd / (n ** 0.5) if (sd > 0 and n > 1) else 0.0
    t_stat = (mean / se) if se > 0 else None
    sharpe = (mean / sd) if sd > 0 else None
    return {"n": n, "pnl": round(sum(pnls), 0),
            "expectancy": round(mean, 1),
            "win_rate": round(100 * len(wins) / n, 1),
            "std": round(sd, 1),
            "t_stat": round(t_stat, 2) if t_stat is not None else None,
            "sharpe": round(sharpe, 3) if sharpe is not None else None}


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
    oos_trades = [
        t for t in trades
        if oos_year and str(t.get("exit_date") or "").startswith(oos_year)
    ]
    green_months = sum(1 for b in months.values() if b["pnl"] > 0)
    pct_green = round(100 * green_months / len(months), 1) if months else 0.0
    all_years_positive = bool(year_keys) and all(years[y]["pnl"] > 0 for y in year_keys)
    oos_positive = bool(oos_year) and years[oos_year]["expectancy"] > 0

    # verdict — P5-J1 (2026-07-23): a positive expectancy is not an edge until it is
    # statistically distinguishable from zero. Serious desks demand |t|>=3 on a NEW
    # signal (the article's rule; CLAUDE.md §20 overfitting law), so CANDIDATE_EDGE now
    # requires the overall t-stat to clear JUDGE_T_STAT_MIN on top of the persistence
    # checks. A positive-but-insignificant result is FRAGILE, not a candidate — this is
    # the fix for "the winning theta cluster was small-sample illusion".
    t_overall = overall.get("t_stat")
    if overall["n"] < 30:
        verdict = "INSUFFICIENT_DATA"
    elif overall["expectancy"] <= 0:
        verdict = "NO_EDGE_NEGATIVE"
    elif t_overall is None or t_overall < T_STAT_MIN:
        verdict = "FRAGILE"        # positive but not significant — noise, not edge
    elif all_years_positive and oos_positive and pct_green >= 55:
        verdict = "CANDIDATE_EDGE"
    else:
        verdict = "FRAGILE"

    return {
        "verdict": verdict, "overall": overall,
        "oos_year": oos_year, "oos": years.get(oos_year, {}),
        "oos_trades": oos_trades,
        "by_year": years, "pct_green_months": pct_green,
        "n_months": len(months), "green_months": green_months,
        "all_years_positive": all_years_positive,
        "t_stat": t_overall, "t_stat_min": T_STAT_MIN,
    }
