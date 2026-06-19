"""Option-priced backtester.

Unlike core/backtest_engine.py (which prices trades on the UNDERLYING index and
so ignores premium, theta and spreads), this engine prices every leg from real
historical option-chain snapshots (db.historical_chains) — CE/PE bid/ask/ltp at
each timestamp. It crosses the spread on the way in AND out, so theta decay and
illiquidity show up as real losses. Supports single_leg, credit_spread and
debit_spread, matching the live structures.

Honest limitations (v1):
  - Candle series is derived from the chain snapshots' `spot` (~5-min cadence),
    so o/h/l/c are flat per bar — range/VWAP-heavy signals are approximate.
  - Fidelity grows with chain history; with only a few days collected the output
    validates the ENGINE, not yet a statistically significant edge.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from safe_exec import safe_run_strategy
from core.metrics import compute_metrics, grade

logger = logging.getLogger("quantg.options_backtest")

IST = timedelta(hours=5, minutes=30)
LOT_SIZES = {"NIFTY": 65, "BANKNIFTY": 30, "SENSEX": 20, "FINNIFTY": 65, "MIDCPNIFTY": 120}
BROKERAGE_PER_LEG = 20.0  # INR per leg, charged on the round trip
EOD_SQUAREOFF = "15:20"


def _ist_str(ts: str) -> str:
    """ISO-UTC snapshot ts -> 'YYYY-MM-DD HH:MM' IST (matches strategy signal dates)."""
    t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (t.astimezone(timezone.utc).replace(tzinfo=None) + IST).strftime("%Y-%m-%d %H:%M")


def _mins(date_str: str) -> int:
    hh, mm = date_str[11:16].split(":")
    return int(hh) * 60 + int(mm)


def _strike_interval(strikes: List[Dict[str, Any]]) -> float:
    vals = sorted({float(s["strike"]) for s in strikes})
    diffs = [b - a for a, b in zip(vals, vals[1:]) if b > a]
    return min(diffs) if diffs else 50.0


def _atm_strike(strikes: List[Dict[str, Any]], spot: float) -> float:
    return min((float(s["strike"]) for s in strikes), key=lambda k: abs(k - spot))


def _leg(snap: Dict[str, Any], strike: float, typ: str) -> Optional[Dict[str, Any]]:
    for s in snap["strikes"]:
        if abs(float(s["strike"]) - strike) < 1e-6:
            return s.get(typ.lower())
    return None


def _fill_price(snap: Dict[str, Any], strike: float, typ: str, side: str) -> Optional[float]:
    """Conservative fill: a BUY pays the ask, a SELL receives the bid. Falls back
    to ltp when depth is missing. Returns None if the leg is unpriceable."""
    leg = _leg(snap, strike, typ)
    if not leg:
        return None
    bid = leg.get("bid") or 0
    ask = leg.get("ask") or 0
    ltp = leg.get("ltp") or 0
    if side == "BUY":
        px = ask or ltp or bid
    else:
        px = bid or ltp or ask
    try:
        px = float(px)
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


class OptionsBacktestEngine:
    def __init__(self, db):
        self.db = db

    async def _load_snaps(self, underlying: str, start: Optional[str], end: Optional[str]) -> List[Dict[str, Any]]:
        q: Dict[str, Any] = {"underlying": underlying.upper()}
        if start or end:
            rng: Dict[str, Any] = {}
            if start:
                rng["$gte"] = start
            if end:
                rng["$lte"] = end
            q["date"] = rng
        snaps = await self.db.historical_chains.find(q).to_list(length=200_000)
        snaps.sort(key=lambda s: str(s.get("ts")))
        return snaps

    async def run(
        self,
        strategy: Dict[str, Any],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        starting_capital: float = 100_000.0,
    ) -> Dict[str, Any]:
        vc = strategy.get("visual_config") or {}
        opt = vc.get("options") or {}
        risk = vc.get("risk") or {}
        underlying = (opt.get("underlying") or vc.get("symbol") or "NIFTY").upper()
        structure = opt.get("structure") or "single_leg"
        lot = LOT_SIZES.get(underlying, 50) * int(opt.get("lots") or 1)
        width_strikes = int(opt.get("spread_width") or 2)

        tp_pct = float(risk.get("target_pct") or risk.get("take_profit_pct") or 11) / 100.0
        sl_pct = float(risk.get("stoploss_pct") or risk.get("stop_loss_pct") or 7) / 100.0
        time_exit_min = int(risk.get("time_exit_minutes") or 20)
        cooldown_min = int(risk.get("cooldown_minutes") or 0)
        max_trades_day = int(risk.get("max_trades_day") or 999)

        snaps = await self._load_snaps(underlying, start_date, end_date)
        if len(snaps) < 30:
            return {"error": f"insufficient chain data for {underlying} ({len(snaps)} snaps)",
                    "underlying": underlying, "structure": structure}

        # Build candle timeline + snapshot index keyed by IST minute (same keys as signal dates)
        candles: List[Dict[str, Any]] = []
        snap_by_date: Dict[str, Dict[str, Any]] = {}
        for s in snaps:
            d = _ist_str(s["ts"])
            if d in snap_by_date:
                continue  # de-dupe same-minute snapshots
            snap_by_date[d] = s
            spot = float(s.get("spot") or 0)
            candles.append({"date": d, "open": spot, "high": spot, "low": spot, "close": spot, "volume": 0})

        code = strategy.get("python_code")
        if not code:
            return {"error": "strategy has no python_code", "underlying": underlying, "structure": structure}
        try:
            signals = safe_run_strategy(code, candles)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"strategy code failed: {exc}", "underlying": underlying, "structure": structure}
        sig_by_date: Dict[str, Dict[str, Any]] = {}
        for sig in (signals or []):
            d = str(sig.get("date"))[:16]
            sig_by_date.setdefault(d, sig)  # first signal at each minute

        trades: List[Dict[str, Any]] = []
        active: Optional[Dict[str, Any]] = None
        last_exit_min_by_day: Dict[str, int] = {}
        trades_today: Dict[str, int] = {}

        for c in candles:
            date = c["date"]
            day = date[:10]
            snap = snap_by_date[date]
            spot = float(snap.get("spot") or 0)

            # ---- manage open position ----
            if active:
                val = self._value(active, snap)
                exit_reason = None
                if val is not None:
                    pnl_per_unit = val - active["entry_basis"] if active["dir"] == 1 else active["entry_basis"] - val
                    rel = pnl_per_unit / active["entry_ref"] if active["entry_ref"] else 0.0
                    held = _mins(date) - active["entry_min"]
                    if rel >= active["tp"]:
                        exit_reason = "TAKE_PROFIT"
                    elif rel <= -active["sl"]:
                        exit_reason = "STOP_LOSS"
                    elif held >= time_exit_min:
                        exit_reason = "TIME_EXIT"
                if day != active["day"] or _mins(date) >= _mins("2026-01-01 " + EOD_SQUAREOFF):
                    exit_reason = exit_reason or "EOD_SQUAREOFF"
                if exit_reason and val is not None:
                    self._close(active, snap, date, val, lot, exit_reason, trades)
                    last_exit_min_by_day[day] = _mins(date)
                    active = None

            # ---- look for entry ----
            if active is None and date in sig_by_date:
                if trades_today.get(day, 0) >= max_trades_day:
                    continue
                if cooldown_min and (last_exit_min_by_day.get(day) is not None) \
                        and (_mins(date) - last_exit_min_by_day[day] < cooldown_min):
                    continue
                if _mins(date) >= _mins("2026-01-01 " + EOD_SQUAREOFF):
                    continue
                pos = self._open(sig_by_date[date], snap, spot, structure, width_strikes, tp_pct, sl_pct, date)
                if pos:
                    active = pos
                    trades_today[day] = trades_today.get(day, 0) + 1

        # close hanging at last snapshot
        if active:
            last = candles[-1]
            val = self._value(active, snap_by_date[last["date"]])
            if val is not None:
                self._close(active, snap_by_date[last["date"]], last["date"], val, lot, "FORCE_CLOSE_END", trades)

        pnls = [t["pnl"] for t in trades]
        metrics = compute_metrics(pnls, starting_capital=starting_capital)
        return {
            "strategy_id": strategy.get("id"),
            "name": strategy.get("name"),
            "underlying": underlying,
            "structure": structure,
            "window": {"start": candles[0]["date"], "end": candles[-1]["date"], "snapshots": len(candles)},
            "grade": grade(metrics),
            **metrics,
            "trades": trades,
        }

    # ---- structure pricing ----

    def _open(self, sig, snap, spot, structure, width_strikes, tp_pct, sl_pct, date) -> Optional[Dict[str, Any]]:
        strikes = snap["strikes"]
        interval = _strike_interval(strikes)
        atm = _atm_strike(strikes, spot)
        direction = (sig.get("direction") or "").upper()  # CE / PE
        action = (sig.get("action") or "").upper()        # BUY / SELL
        bullish = action == "BUY" or direction == "CE"

        if structure == "single_leg":
            typ = direction if direction in ("CE", "PE") else ("CE" if bullish else "PE")
            entry = _fill_price(snap, atm, typ, "BUY")
            if not entry:
                return None
            return {"structure": structure, "legs": [("BUY", atm, typ)], "dir": 1,
                    "entry_basis": entry, "entry_ref": entry, "tp": tp_pct, "sl": sl_pct,
                    "entry_min": _mins(date), "day": date[:10], "entry_date": date,
                    "desc": f"BUY {atm:.0f}{typ}"}

        if structure == "credit_spread":
            # bullish -> sell put spread; bearish -> sell call spread
            typ = "PE" if bullish else "CE"
            short_k = atm
            long_k = atm - width_strikes * interval if bullish else atm + width_strikes * interval
            short_px = _fill_price(snap, short_k, typ, "SELL")
            long_px = _fill_price(snap, long_k, typ, "BUY")
            if not short_px or long_px is None:
                return None
            credit = short_px - long_px
            if credit <= 0:
                return None
            width_pts = abs(short_k - long_k)
            return {"structure": structure, "short_k": short_k, "long_k": long_k, "typ": typ, "dir": 1,
                    "entry_basis": credit, "entry_ref": credit, "tp": 0.5, "sl": 1.0,  # +50% capture / -100% of credit
                    "max_loss_pts": width_pts - credit,
                    "entry_min": _mins(date), "day": date[:10], "entry_date": date,
                    "desc": f"SELL {short_k:.0f}/{long_k:.0f}{typ} credit={credit:.2f}"}

        if structure == "debit_spread":
            # bullish -> buy call spread; bearish -> buy put spread
            typ = "CE" if bullish else "PE"
            long_k = atm
            short_k = atm + width_strikes * interval if bullish else atm - width_strikes * interval
            long_px = _fill_price(snap, long_k, typ, "BUY")
            short_px = _fill_price(snap, short_k, typ, "SELL")
            if not long_px or short_px is None:
                return None
            debit = long_px - short_px
            if debit <= 0:
                return None
            return {"structure": structure, "short_k": short_k, "long_k": long_k, "typ": typ, "dir": 1,
                    "entry_basis": debit, "entry_ref": debit, "tp": 0.5, "sl": 0.5,
                    "entry_min": _mins(date), "day": date[:10], "entry_date": date,
                    "desc": f"BUY {long_k:.0f}/{short_k:.0f}{typ} debit={debit:.2f}"}
        return None

    def _value(self, pos: Dict[str, Any], snap: Dict[str, Any]) -> Optional[float]:
        """Current close-out basis (premium for single leg; spread value for spreads),
        priced conservatively (you cross the spread to exit)."""
        s = pos["structure"]
        if s == "single_leg":
            _, k, typ = pos["legs"][0]
            return _fill_price(snap, k, typ, "SELL")  # sell to close a long
        if s == "credit_spread":
            short_close = _fill_price(snap, pos["short_k"], pos["typ"], "BUY")   # buy back short
            long_close = _fill_price(snap, pos["long_k"], pos["typ"], "SELL")    # sell the long
            if short_close is None or long_close is None:
                return None
            return short_close - long_close  # cost to close the spread
        if s == "debit_spread":
            long_close = _fill_price(snap, pos["long_k"], pos["typ"], "SELL")
            short_close = _fill_price(snap, pos["short_k"], pos["typ"], "BUY")
            if long_close is None or short_close is None:
                return None
            return long_close - short_close  # spread value on exit
        return None

    def _close(self, pos, snap, date, val, lot, reason, trades):
        s = pos["structure"]
        if s == "single_leg":
            pnl_per_unit = val - pos["entry_basis"]  # long
        elif s == "credit_spread":
            pnl_per_unit = pos["entry_basis"] - val  # credit received - cost to close
        else:  # debit_spread
            pnl_per_unit = val - pos["entry_basis"]
        legs = 1 if s == "single_leg" else 2
        gross = pnl_per_unit * lot
        costs = BROKERAGE_PER_LEG * legs * 2  # round trip both legs
        trades.append({
            "structure": s, "desc": pos["desc"], "entry_date": pos["entry_date"], "exit_date": date,
            "entry_basis": round(pos["entry_basis"], 2), "exit_basis": round(val, 2),
            "exit_reason": reason, "gross_pnl": round(gross, 2),
            "pnl": round(gross - costs, 2),
        })
