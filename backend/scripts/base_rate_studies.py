#!/usr/bin/env python3
"""Base-rate studies on the bhavcopy data — WHERE could edge live?

Not strategy backtests. Direct questions to 2 years of real option prices, to
find a foundation BEFORE designing new strategies:

  A. Short ATM straddle held to expiry — the purest volatility-risk-premium test.
     If selling ATM vol makes money on average, there IS a harvestable edge and
     the old book's problem was HOW it sold, not THAT it sold.
  B. Short OTM strangle held to expiry — defined-direction-agnostic vol selling
     at a couple of widths (does moving OTM help or hurt?).
  C. Regime split of A — does short-vol pay MORE when the market is ranging
     (low efficiency ratio) vs trending? (the classic theta-seller intuition)
  D. Directional momentum base rate on the underlying — is there trend to follow?

Costs modelled realistically for a HOLD-TO-EXPIRY seller: you cross the spread
only on ENTRY (SLIP per leg); expiry is cash-settled at exact intrinsic (no exit
spread). Payoff uses the index (futures close) intrinsic — NSE/BSE index options
are cash-settled. Reports POINTS (lot-independent) and ₹ (× lot).

Run:  docker exec quantg-backend python /app/scripts/base_rate_studies.py
"""
import os
import sys
import statistics as st
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.bhavcopy_store import BhavcopyStore  # noqa: E402

UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"]
LOT = {"NIFTY": 75, "BANKNIFTY": 35, "SENSEX": 20, "BANKEX": 15}
SLIP = float(os.environ.get("STUDY_SLIP_PCT", "0.015"))   # entry slippage per leg
ENTRY_DTE = (2, 8)   # enter into the nearest expiry this many days out (weekly)


def _dte(a, b):
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0.0


def efficiency_ratio(closes, i, n=10):
    """Kaufman ER over n days ending at i: |net move| / sum|daily moves|. ~1 = trend, ~0 = chop."""
    if i < n:
        return None
    net = abs(closes[i] - closes[i - n])
    noise = sum(abs(closes[j] - closes[j - 1]) for j in range(i - n + 1, i + 1))
    return net / noise if noise > 0 else 0.0


def run_study(store, u, otm_pct=0.0):
    """Short straddle (otm_pct=0) or strangle (otm_pct>0) to expiry. Returns list of
    per-cycle dicts with pnl in points, plus the entry regime ER."""
    candles = store.underlying_daily(u)
    if len(candles) < 60:
        return []
    close_by = {c["date"][:10]: c["close"] for c in candles}
    days = [c["date"][:10] for c in candles]
    closes = [c["close"] for c in candles]
    lot = LOT.get(u, 50)
    out = []
    entered = set()
    for i, day in enumerate(days):
        exps = store.expiries(u, day)
        pick = None
        for e in sorted(exps):
            d = _dte(day, e)
            if ENTRY_DTE[0] <= d <= ENTRY_DTE[1]:
                pick = e
                break
        if not pick or pick in entered:
            continue
        chain = store.option_chain(u, day).get(pick, {})
        if not chain:
            continue
        strikes = sorted(chain.keys())
        if len(strikes) < 5:
            continue
        spot = close_by[day]
        interval = min((b - a for a, b in zip(strikes, strikes[1:]) if b > a), default=50)
        ce_k = min(strikes, key=lambda k: abs(k - spot * (1 + otm_pct)))
        pe_k = min(strikes, key=lambda k: abs(k - spot * (1 - otm_pct)))
        ce = store.leg_settle(u, day, pick, ce_k, "CE")
        pe = store.leg_settle(u, day, pick, pe_k, "PE")
        if not ce or not pe:
            continue
        credit = ce + pe
        credit_net = credit * (1 - SLIP)   # cross the spread selling both legs
        # payoff at expiry: cash-settled intrinsic vs the index close on expiry day
        s_exp = close_by.get(pick)
        if s_exp is None:                  # expiry not a stored trading day -> nearest prior
            prior = [d for d in days if d <= pick]
            if not prior:
                continue
            s_exp = close_by[prior[-1]]
        payoff = max(0.0, s_exp - ce_k) + max(0.0, pe_k - s_exp)
        pnl_pts = credit_net - payoff      # short: keep credit, pay intrinsic
        er = efficiency_ratio(closes, i)
        entered.add(pick)
        out.append({"day": day, "expiry": pick, "credit": credit, "pnl_pts": pnl_pts,
                    "rupees": pnl_pts * lot, "er": er})
    return out


def summarize(name, rows):
    if not rows:
        print(f"  {name:<34} (no cycles)")
        return
    pnls = [r["pnl_pts"] for r in rows]
    rup = [r["rupees"] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    print(f"  {name:<34} n={len(rows):>3}  meanPts={st.mean(pnls):>8.1f}  "
          f"WR={100*wins/len(rows):>4.0f}%  mean₹={st.mean(rup):>9.0f}  "
          f"sum₹={sum(rup):>11.0f}  worstPts={min(pnls):>7.0f}")


def main():
    store = BhavcopyStore()
    days = store.trading_days()
    print(f"bhavcopy window: {days[0]} -> {days[-1]}  ({len(days)} days)  slip/leg={SLIP:.1%}\n")

    print("=" * 100)
    print("STUDY A & B — SHORT vol held to expiry (positive = selling vol PAYS). meanPts/mean₹ are PER CYCLE.")
    print("=" * 100)
    straddles = {}
    for u in UNDERLYINGS:
        print(f"\n{u}  (lot={LOT.get(u)})")
        s0 = run_study(store, u, 0.0);   straddles[u] = s0
        summarize("A. short ATM straddle", s0)
        summarize("B. short strangle ~1% OTM", run_study(store, u, 0.01))
        summarize("B. short strangle ~2% OTM", run_study(store, u, 0.02))

    print("\n" + "=" * 100)
    print("STUDY C — short ATM straddle split by REGIME at entry (ER<0.35 = RANGE, ER>0.55 = TREND)")
    print("=" * 100)
    for u in UNDERLYINGS:
        rows = [r for r in straddles[u] if r["er"] is not None]
        rng = [r for r in rows if r["er"] < 0.35]
        trn = [r for r in rows if r["er"] > 0.55]
        print(f"\n{u}")
        summarize("  RANGE entries (ER<0.35)", rng)
        summarize("  TREND entries (ER>0.55)", trn)

    print("\n" + "=" * 100)
    print("STUDY D — directional momentum base rate on the underlying (does trend continue?)")
    print("=" * 100)
    for u in UNDERLYINGS:
        candles = store.underlying_daily(u)
        closes = [c["close"] for c in candles]
        cont = tot = 0
        fwd_ret = []
        for i in range(20, len(closes) - 5):
            up = closes[i] > closes[i - 20]                 # 20-day momentum
            f = (closes[i + 5] - closes[i]) / closes[i]     # forward 5-day return
            fwd_ret.append(f if up else -f)                 # return in the momentum direction
            tot += 1
            if (f > 0) == up:
                cont += 1
        if tot:
            print(f"  {u:<10} momentum-continuation WR={100*cont/tot:>4.0f}%  "
                  f"mean fwd-5d return in trend dir={100*st.mean(fwd_ret):>+5.2f}%  (n={tot})")


if __name__ == "__main__":
    main()
