"""Migration: fix the 10 NSE_EQ equity strategies (2026-06-23).

Two build-bugs fixed across all equity strategies:

  1. ENTRIES GET DROPPED. The original code emitted an entry only on a *momentary*
     condition (e.g. EMA9 crossing EMA21 on one bar) guarded by an internal
     position state machine. The runner only acts on a signal whose date is in the
     last 1-2 candles (_select_latest_recent_signal), so a one-bar entry that
     didn't land in that window was silently discarded — RELIANCE/SBIN/INFY took 0
     trades all day despite live data. FIX: emit the entry on EVERY bar while the
     entry *state* holds (re-entry pattern); the runner's "active-position" gate +
     max_trades_day(3) + 12m cooldown still prevent over-trading.

  2. SL/TP NEVER TRIGGER. Config used stop_loss=7% / take_profit=11% — options
     sizing. A large-cap rarely moves that intraday, so positions drifted to the
     15:10 square-off at ±~Rs100. FIX: each entry now carries ATR(14)-based
     stop_loss/take_profit PRICES (1.5x ATR stop / 3.0x ATR target). signal_manager
     uses per-signal prices in preference to config %, and portfolio_ledger stamps
     them as absolute sl_price/tp_price so the monitor enforces real, volatility-
     scaled exits.

Run (validate only, no DB):   python fix_equity_strategies_atr.py
Run (apply to mongo):         python fix_equity_strategies_atr.py --apply
(Apply must run where pymongo can reach mongo, e.g. inside the backend container.)
"""
import os
import sys

ATR_STOP_MULT = 1.5
ATR_TARGET_MULT = 3.0

EMA_DEF = (
    "    def ema(values, period):\n"
    "        k = 2.0 / (period + 1); out = [values[0]]\n"
    "        for val in values[1:]: out.append(val * k + out[-1] * (1 - k))\n"
    "        return out\n"
)
RSI_DEF = (
    "    rsi = [50.0] * len(closes)\n"
    "    for j in range(14, len(closes)):\n"
    "        g = sum(max(closes[k] - closes[k-1], 0) for k in range(j-13, j+1))\n"
    "        l = sum(max(closes[k-1] - closes[k], 0) for k in range(j-13, j+1)) or 0.0001\n"
    "        rsi[j] = 100 - (100 / (1 + g / l))\n"
)
VWAP_DEF = (
    "    cum_pv = 0.0; cum_v = 0.0; vwap = [0.0] * len(closes)\n"
    "    for j in range(len(closes)):\n"
    "        typ = (highs[j] + lows[j] + closes[j]) / 3.0\n"
    "        cum_pv += typ * vols[j]; cum_v += vols[j]\n"
    "        vwap[j] = cum_pv / max(1.0, cum_v)\n"
)


def build(minbars, start, prelude, loop_pre, entry_expr, exit_expr,
          setup, entry_reason, exit_reason, maxhold, side="LONG"):
    entry_action = "BUY" if side == "LONG" else "SELL"
    exit_action = "SELL" if side == "LONG" else "BUY"
    if side == "LONG":
        sl_expr = f"price - {ATR_STOP_MULT}*a"
        tp_expr = f"price + {ATR_TARGET_MULT}*a"
    else:
        sl_expr = f"price + {ATR_STOP_MULT}*a"
        tp_expr = f"price - {ATR_TARGET_MULT}*a"
    lp = ("        " + loop_pre + "\n") if loop_pre else ""
    return (
        "def run(data):\n"
        f"    if len(data) < {minbars}: return []\n"
        "    closes = [float(d['close']) for d in data]\n"
        "    highs = [float(d.get('high', d['close'])) for d in data]\n"
        "    lows = [float(d.get('low', d['close'])) for d in data]\n"
        "    vols = [max(1.0, float(d.get('volume') or 1)) for d in data]\n"
        f"{prelude}"
        "    trs = [0.0]\n"
        "    for j in range(1, len(closes)):\n"
        "        trs.append(max(highs[j]-lows[j], abs(highs[j]-closes[j-1]), abs(lows[j]-closes[j-1])))\n"
        "    atr = [0.0] * len(closes)\n"
        "    for j in range(len(closes)):\n"
        "        atr[j] = (sum(trs[j-13:j+1])/14.0) if j >= 14 else (sum(trs[1:j+1])/max(1, j) if j > 0 else 0.0)\n"
        "    signals = []\n"
        f"    for i in range({start}, len(data)):\n"
        "        clock = str(data[i].get('date', ''))[11:16]\n"
        "        price = closes[i]; a = atr[i] if atr[i] > 0 else price * 0.005\n"
        "        if clock and clock > '15:05':\n"
        f"            signals.append({{\"date\": data[i][\"date\"], \"action\": \"{exit_action}\", \"setup_type\": \"{setup}\", \"confidence\": 50.0, \"entry_reason\": \"Time exit\", \"signal_version\": \"v13\", \"strategy_logic_version\": \"2.0\"}})\n"
        "            continue\n"
        f"{lp}"
        f"        if {entry_expr}:\n"
        f"            signals.append({{\"date\": data[i][\"date\"], \"action\": \"{entry_action}\", \"setup_type\": \"{setup}\", \"confidence\": 80.0, \"entry_reason\": \"{entry_reason}\", \"stop_loss\": round({sl_expr}, 2), \"take_profit\": round({tp_expr}, 2), \"max_hold_minutes\": {maxhold}, \"signal_version\": \"v13\", \"strategy_logic_version\": \"2.0\"}})\n"
        f"        elif {exit_expr}:\n"
        f"            signals.append({{\"date\": data[i][\"date\"], \"action\": \"{exit_action}\", \"setup_type\": \"{setup}\", \"confidence\": 70.0, \"entry_reason\": \"{exit_reason}\", \"signal_version\": \"v13\", \"strategy_logic_version\": \"2.0\"}})\n"
        "    return signals\n"
    )


STRATS = {
    # RELIANCE — EMA9/21 trend
    "d656c981-141a-4b0b-bcee-7d2e14e99702": build(
        55, 30, EMA_DEF + "    ema9 = ema(closes, 9); ema21 = ema(closes, 21)\n", "",
        "ema9[i] > ema21[i]", "ema9[i] < ema21[i]",
        "trend_follow", "EMA9>EMA21 uptrend (re-entry)", "EMA trend flipped down (exit)", 180),
    # SBIN — VWAP/EMA50 bearish breakdown (SHORT)
    "9711f315-c816-41db-8f04-4186afe28863": build(
        55, 50, EMA_DEF + VWAP_DEF + "    ema50 = ema(closes, 50)\n", "",
        "closes[i] < vwap[i] and closes[i] < ema50[i] and closes[i] <= closes[i-1]",
        "closes[i] > vwap[i] or closes[i] > ema50[i]",
        "bearish_breakdown", "Below VWAP & EMA50 breakdown (short re-entry)", "Reclaimed VWAP/EMA (cover)", 180, side="SHORT"),
    # HDFCBANK — RSI + Bollinger mean reversion
    "fab2101b-cb43-4d49-90da-59634c210f5b": build(
        30, 20, RSI_DEF,
        "chunk = closes[i-20:i]; sma = sum(chunk)/20.0; std = (sum((x-sma)**2 for x in chunk)/20.0)**0.5; lower_band = sma - 2*std",
        "rsi[i] < 35 and closes[i] <= lower_band",
        "closes[i] >= sma or rsi[i] > 65",
        "mean_reversion", "Oversold below lower band (re-entry)", "Reverted to mean (exit)", 120),
    # ICICIBANK — volatility/recent-high breakout
    "c6b5c5dc-71cc-467d-83ec-b64a1757d731": build(
        30, 20, "", "",
        "closes[i] >= max(highs[i-6:i]) * 0.997 and closes[i] > closes[i-1]",
        "closes[i] < min(lows[i-20:i])",
        "volatility_breakout", "Near recent high w/ momentum (re-entry)", "Donchian breakdown (exit)", 120),
    # TCS — RSI defensive accumulation
    "881ceec0-7ecd-492b-9528-e8fa00f01624": build(
        30, 20, RSI_DEF, "",
        "rsi[i] < 30", "rsi[i] > 60",
        "defensive_accumulation", "RSI oversold accumulation (re-entry)", "RSI recovered (exit)", 240),
    # INFY — trend above VWAP (was pullback)
    "7bdfe52a-50f0-4c2b-8a5c-daff5e2e63ee": build(
        55, 50, EMA_DEF + VWAP_DEF + "    ema50 = ema(closes, 50)\n", "",
        "closes[i] > ema50[i] and closes[i] > vwap[i]",
        "closes[i] < ema50[i]",
        "pullback", "Uptrend above VWAP (re-entry)", "Lost EMA50 trend (exit)", 180),
    # AXISBANK — macro EMA200 + EMA9/21
    "5b8f0e2d-4eb4-4676-a762-e79b7c795506": build(
        210, 200, EMA_DEF + "    ema9 = ema(closes, 9); ema21 = ema(closes, 21); ema200 = ema(closes, 200)\n", "",
        "ema200[i] > ema200[i-1] and ema9[i] > ema21[i]",
        "ema9[i] < ema21[i]",
        "trend_follow", "Macro up + EMA9>EMA21 (re-entry)", "EMA trend flipped down (exit)", 180),
    # LT — Donchian breakout
    "e379d528-e3f0-4f4c-bf8c-15e9204a160c": build(
        30, 20, "",
        "dh = max(highs[i-12:i]); dl = min(lows[i-12:i]); rmid = (dh+dl)/2.0",
        "closes[i] >= dh * 0.999 or (closes[i] > rmid and closes[i] > closes[i-1])",
        "closes[i] < dl",
        "breakout", "Donchian momentum (re-entry)", "Donchian breakdown (exit)", 180),
    # BHARTIARTL — EMA20/50 trend
    "020a9214-ce7e-4dee-bb0c-892134053634": build(
        55, 50, EMA_DEF + "    ema20 = ema(closes, 20); ema50 = ema(closes, 50)\n", "",
        "ema20[i] > ema50[i] and closes[i] >= ema20[i] * 0.997",
        "ema20[i] < ema50[i]",
        "defensive_trend", "EMA20>EMA50 trend (re-entry)", "EMA trend flipped down (exit)", 180),
    # KOTAKBANK — RSI rebound swing
    "3381b22b-b096-4d2e-b12e-ed0228fc38cd": build(
        30, 20, RSI_DEF, "",
        "rsi[i] >= 34 and rsi[i] <= 60 and closes[i] >= closes[i-1]",
        "rsi[i] > 65",
        "rsi_swing", "RSI rebound from value zone (re-entry)", "RSI overbought (exit)", 240),
}


import math


def _series(kind: int):
    """260 bars. kind=+1 up-then-down, -1 down-then-up (shorts), 0 oscillating
    (exercises RSI/Bollinger mean-reversion)."""
    bars = []
    base = 1000.0
    for n in range(260):
        if kind == 0:
            close = 1000.0 + 40.0 * math.sin(n / 8.0)
        else:
            base += 1.2 * kind   # monotonic up (kind=+1) or down (kind=-1)
            close = base
        # 75 five-min bars per session (09:15..15:25), cycling across days
        slot = n % 75
        day = 10 + (n // 75)
        tmin = 9 * 60 + 15 + slot * 5
        hh, mm = tmin // 60, tmin % 60
        bars.append({"date": f"2026-06-{day:02d} {hh:02d}:{mm:02d}", "close": close,
                     "high": close + 2, "low": close - 2, "volume": 10000})
    return bars


def _smoke(code: str, label: str):
    """Compile + run on both an up-trend and a down-trend series; ensure no
    exception and that at least one direction produces ATR-bracketed entries."""
    compile(code, f"<{label}>", "exec")
    ns: dict = {}
    exec(code, ns)
    total = 0
    best_entries = []
    for kind in (1, -1, 0):
        out = ns["run"](_series(kind))
        assert isinstance(out, list), f"{label}: run() did not return a list"
        total = max(total, len(out))
        ents = [s for s in out if s.get("stop_loss") is not None]
        if len(ents) > len(best_entries):
            best_entries = ents
    assert best_entries, f"{label}: produced no entry signals on either trend"
    e = best_entries[len(best_entries) // 2]
    assert e.get("take_profit") is not None and e.get("action") in ("BUY", "SELL")
    return total, len(best_entries)


def main():
    apply = "--apply" in sys.argv
    for sid, code in STRATS.items():
        n, ne = _smoke(code, sid[:8])
        print(f"OK  {sid[:8]}  signals={n:4d}  entries_with_atr={ne}")
    print(f"\nAll {len(STRATS)} strategies generated + smoke-tested.")
    if not apply:
        print("Validate-only. Re-run with --apply to write to mongo.")
        return
    from pymongo import MongoClient
    url = os.environ.get("MONGO_URL") or os.environ.get("MONGO_URI") or "mongodb://mongo:27017"
    db = MongoClient(url).get_database("quantg")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    for sid, code in STRATS.items():
        res = db.strategies.update_one(
            {"id": sid},
            {"$set": {"python_code": code, "strategy_logic_version": "2.0",
                      "code_migrated_at": now}},
        )
        print(f"APPLIED {sid[:8]}  matched={res.matched_count} modified={res.modified_count}")


if __name__ == "__main__":
    main()
