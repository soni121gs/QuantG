#!/usr/bin/env python3
"""EDR-07 — does the short-OTM-vol edge SURVIVE once we cap the tail with wings?

base_rate_studies.py proved selling a ~2% OTM strangle to expiry is the one real
edge (SENSEX +₹2k/cycle, 82% WR) — but NAKED, with a fat left tail (worst single
cycle −₹43-48k). This study re-runs the SAME sell-and-hold-to-expiry cycle as a
DEFINED-RISK iron condor: sell the ~2% OTM strangle, BUY further-OTM wings. The
wings give back some credit to cap the disaster. The question: after paying for
them, is it still +EV — and is the worst cycle actually bounded?

Held to weekly expiry, cash-settled at exact index intrinsic. Entry crosses the
spread on all 4 legs (SLIP/leg); expiry has no exit spread. Reports POINTS
(lot-independent) and ₹ (× lot), per wing width, next to the naked reference.

Run:  docker exec quantg-backend python /app/scripts/condor_study.py
"""
import os
import sys
import statistics as st
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.bhavcopy_store import BhavcopyStore  # noqa: E402

UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"]
LOT = {"NIFTY": 75, "BANKNIFTY": 35, "SENSEX": 20, "BANKEX": 15}
SLIP = float(os.environ.get("STUDY_SLIP_PCT", "0.015"))    # entry slippage per leg
SHORT_OTM = float(os.environ.get("CONDOR_SHORT_OTM", "0.02"))  # short legs ~2% OTM
WING_WIDTHS = [2, 4, 6, 10]                                 # strikes beyond the shorts
ENTRY_DTE = (2, 8)


def _dte(a, b):
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def run_condor(store, u, wing_strikes, otm=SHORT_OTM):
    """One iron condor per weekly expiry, held to settlement. wing_strikes=0 => the
    naked short strangle (the reference). Returns per-cycle rows."""
    candles = store.underlying_daily(u)
    if len(candles) < 60:
        return []
    close_by = {c["date"][:10]: c["close"] for c in candles}
    days = [c["date"][:10] for c in candles]
    lot = LOT.get(u, 50)
    out, entered = [], set()
    for day in days:
        pick = None
        for e in sorted(store.expiries(u, day)):
            if ENTRY_DTE[0] <= _dte(day, e) <= ENTRY_DTE[1]:
                pick = e
                break
        if not pick or pick in entered:
            continue
        chain = store.option_chain(u, day).get(pick, {})
        strikes = sorted(chain.keys())
        if len(strikes) < 7:
            continue
        interval = min((b - a for a, b in zip(strikes, strikes[1:]) if b > a), default=50)
        spot = close_by[day]
        short_dist = max(1, round(spot * otm / interval))
        ce_s_k = min(strikes, key=lambda k: abs(k - (spot + short_dist * interval)))
        pe_s_k = min(strikes, key=lambda k: abs(k - (spot - short_dist * interval)))
        ce_l_k = ce_s_k + wing_strikes * interval if wing_strikes else None
        pe_l_k = pe_s_k - wing_strikes * interval if wing_strikes else None

        ce_s = store.leg_settle(u, day, pick, ce_s_k, "CE")
        pe_s = store.leg_settle(u, day, pick, pe_s_k, "PE")
        if not ce_s or not pe_s:
            continue
        credit = (ce_s + pe_s) * (1 - SLIP)
        cap = None
        if wing_strikes:
            ce_l = store.leg_settle(u, day, pick, ce_l_k, "CE")
            pe_l = store.leg_settle(u, day, pick, pe_l_k, "PE")
            if ce_l is None or pe_l is None:
                continue
            credit -= (ce_l + pe_l) * (1 + SLIP)   # buy the wings (pay up)
            cap = wing_strikes * interval           # max loss per side = wing points - credit
        if credit <= 0:
            continue

        s_exp = close_by.get(pick)
        if s_exp is None:
            prior = [d for d in days if d <= pick]
            if not prior:
                continue
            s_exp = close_by[prior[-1]]

        ce_pay = max(0.0, s_exp - ce_s_k)
        pe_pay = max(0.0, pe_s_k - s_exp)
        if wing_strikes:
            ce_pay -= max(0.0, s_exp - ce_l_k)     # wing caps the call side
            pe_pay -= max(0.0, pe_l_k - s_exp)      # wing caps the put side
        pnl_pts = credit - (ce_pay + pe_pay)
        entered.add(pick)
        out.append({"pnl_pts": pnl_pts, "rupees": pnl_pts * lot,
                    "max_loss_pts": (cap - credit) if cap else None})
    return out


def summarize(label, rows):
    if not rows:
        print(f"    {label:<24} (no cycles)")
        return
    pnls = [r["pnl_pts"] for r in rows]
    rup = [r["rupees"] for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    caps = [r["max_loss_pts"] for r in rows if r["max_loss_pts"] is not None]
    cap_txt = f"  capMaxLoss≈{st.mean(caps):>6.0f}pts" if caps else "  (uncapped)"
    print(f"    {label:<24} n={len(rows):>3}  mean₹={st.mean(rup):>8.0f}  "
          f"WR={100*wins/len(rows):>4.0f}%  sum₹={sum(rup):>10.0f}  "
          f"worst₹={min(rup):>9.0f}{cap_txt}")


def main():
    store = BhavcopyStore()
    days = store.trading_days()
    print(f"bhavcopy {days[0]} -> {days[-1]} ({len(days)}d) | short ~{SHORT_OTM:.0%} OTM | "
          f"slip {SLIP:.1%}/leg | held to expiry\n")
    print("=" * 104)
    print("EDR-07 — DEFINED-RISK CONDOR vs NAKED STRANGLE (does the tail-cap keep the edge?)")
    print("=" * 104)
    for u in UNDERLYINGS:
        print(f"\n{u}  (lot={LOT.get(u)})")
        summarize("NAKED strangle (ref)", run_condor(store, u, 0))
        for w in WING_WIDTHS:
            summarize(f"condor wing={w} strikes", run_condor(store, u, w))
    print("\nRead: mean₹ should stay positive (edge survives) while worst₹ shrinks toward")
    print("capMaxLoss (tail bounded). If wings crush mean₹ negative, the edge does not")
    print("survive defined risk at that width — widen the wings or move the shorts.")


if __name__ == "__main__":
    main()
