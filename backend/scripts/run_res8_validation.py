"""RES-8 CLI — grade the seller brain vs the IV-cheap buyer, side by side.

    docker exec -w /app quantg-backend python scripts/run_res8_validation.py
    docker exec -w /app quantg-backend python scripts/run_res8_validation.py --underlying SENSEX

Data must be at /app/data/bhavcopy_fo inside the container.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.res8_oos import run_res8


def _fmt(v):
    o = v.get("overall") or {}
    oos = v.get("oos") or {}
    if v.get("verdict") == "ERROR":
        return f"  ERROR: {v.get('error')}  (signals={v.get('n_signals')})"
    return (
        f"  verdict           : {v['verdict']}\n"
        f"  signals / trades  : {v.get('n_signals')} / {o.get('n')}\n"
        f"  expectancy (all)  : Rs {o.get('expectancy')}/trade   total Rs {o.get('pnl')}\n"
        f"  OOS year {v.get('oos_year')}     : expectancy Rs {oos.get('expectancy')}/trade "
        f"(n={oos.get('n')}, pnl Rs {oos.get('pnl')})\n"
        f"  all years +ve     : {v.get('all_years_positive')}   green months: {v.get('pct_green_months')}%\n"
        f"  by year           : "
        + ", ".join(f"{y}: Rs {b.get('expectancy')}/tr (n={b.get('n')})"
                    for y, b in (v.get('by_year') or {}).items())
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2025-12-31")
    ap.add_argument("--min-edge", type=float, default=2.0)
    args = ap.parse_args()

    out = run_res8(args.underlying, args.start, args.end, args.min_edge)
    print("=" * 74)
    print(f"RES-8 OOS VALIDATION — {out['underlying']}  "
          f"{out['window']['start']}..{out['window']['end']}  (IV-RV min edge {out['min_edge']} vol pts)")
    print("=" * 74)
    print("\nSELLER — premium RICH + RANGE (RES-7 gate), width-1 credit spread held to theta:")
    print(_fmt(out["seller"]))
    print("\nBUYER — premium CHEAP + TRENDING (inverse gate), debit spread held to theta:")
    print(_fmt(out["buyer"]))
    print("\n" + "-" * 74)
    sv, bv = out["seller"].get("verdict"), out["buyer"].get("verdict")
    print(f"SIDE BY SIDE:  seller = {sv}   |   buyer = {bv}")
    print("-" * 74)


if __name__ == "__main__":
    main()
