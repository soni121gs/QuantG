"""P5-M4 (2026-08-02): alpha-vs-beta separation for the whole book.

The §20 census says the QuantG book is "one bet expressed 11 ways" — short index
premium. This module answers the question that framing implies but never tested:
**is any strategy's P&L alpha, or is it just the short-volatility risk premium
(beta) that any ATM straddle seller would have earned, minus costs?**

Method (Grinold-Kahn framing, IR = IC·√BR):
  1. Build a daily SHORT-VOL BENCHMARK from the bhavcopy store — sell the ATM
     straddle at the close, hold `hold_days`, mark the P&L as a return on the
     collected premium. This is the pure premium-selling factor.
  2. Regress each strategy's daily returns on that benchmark AND the NIFTY daily
     return (two factors): r_strat = α + β_sv·r_sv + β_mkt·r_mkt + ε.
  3. Report α (the part NOT explained by short-vol or market beta), its t-stat,
     the betas, and R². **If β_sv ≈ 1 and α ≈ 0 the strategy is replicable risk
     premium the book pays costs to reproduce** — that finding redirects the
     program away from re-parameterising the one bet.

Pure: the regression + benchmark are I/O-free given a store and series. The DB
fetch of per-strategy daily returns lives in the CLI (`scripts/run_alpha_beta.py`)
because it needs the live trade_fills ledger; this core is unit-testable offline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---- tiny OLS (no numpy dependency) ----------------------------------------

def _matmul_at_a(cols: List[List[float]]) -> List[List[float]]:
    k = len(cols)
    return [[sum(cols[i][t] * cols[j][t] for t in range(len(cols[0])))
             for j in range(k)] for i in range(k)]


def _mat_vec_at_y(cols: List[List[float]], y: Sequence[float]) -> List[float]:
    return [sum(cols[i][t] * y[t] for t in range(len(y))) for i in range(len(cols))]


def _invert(m: List[List[float]]) -> Optional[List[List[float]]]:
    """Gauss-Jordan inverse of a small square matrix. None if singular."""
    n = len(m)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            return None
        a[col], a[piv] = a[piv], a[col]
        pv = a[col][col]
        a[col] = [v / pv for v in a[col]]
        for r in range(n):
            if r == col:
                continue
            f = a[r][col]
            if f:
                a[r] = [a[r][c] - f * a[col][c] for c in range(2 * n)]
    return [row[n:] for row in a]


@dataclass
class Regression:
    n: int
    alpha: float                       # intercept (per-day, same units as y)
    alpha_t: Optional[float]
    betas: Dict[str, float] = field(default_factory=dict)
    beta_t: Dict[str, Optional[float]] = field(default_factory=dict)
    r_squared: float = 0.0
    verdict: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"n": self.n, "alpha": round(self.alpha, 5),
                "alpha_t": (round(self.alpha_t, 2) if self.alpha_t is not None else None),
                "betas": {k: round(v, 4) for k, v in self.betas.items()},
                "beta_t": {k: (round(v, 2) if v is not None else None)
                           for k, v in self.beta_t.items()},
                "r_squared": round(self.r_squared, 4), "verdict": self.verdict}


def ols(y: Sequence[float], factors: Dict[str, Sequence[float]]) -> Optional[Regression]:
    """Multiple OLS of y on the named factors plus an intercept. Reports HAC-free
    classical t-stats (P5-J5's Newey-West lives in the EOD judge; here the sample is
    daily aligned returns and the classical error is adequate for the α/β screen).
    Returns None when the design is rank-deficient or n is too small."""
    names = list(factors.keys())
    n = len(y)
    if n < len(names) + 2:
        return None
    # design columns: intercept first
    cols: List[List[float]] = [[1.0] * n] + [[float(v) for v in factors[nm]] for nm in names]
    if any(len(c) != n for c in cols):
        return None
    xtx = _matmul_at_a(cols)
    inv = _invert(xtx)
    if inv is None:
        return None
    xty = _mat_vec_at_y(cols, y)
    coef = [sum(inv[i][j] * xty[j] for j in range(len(xty))) for i in range(len(inv))]
    # residuals + variance
    resid = [y[t] - sum(coef[i] * cols[i][t] for i in range(len(cols))) for t in range(n)]
    dof = n - len(cols)
    sse = sum(r * r for r in resid)
    sigma2 = sse / dof if dof > 0 else 0.0
    ybar = sum(y) / n
    sst = sum((v - ybar) ** 2 for v in y)
    r2 = 1.0 - sse / sst if sst > 0 else 0.0
    # standard errors = sqrt(sigma2 * diag(inv))
    def _t(i: int) -> Optional[float]:
        se2 = sigma2 * inv[i][i]
        if se2 <= 0:
            return None
        return coef[i] / (se2 ** 0.5)
    betas = {nm: coef[i + 1] for i, nm in enumerate(names)}
    beta_t = {nm: _t(i + 1) for i, nm in enumerate(names)}
    return Regression(n=n, alpha=coef[0], alpha_t=_t(0),
                      betas=betas, beta_t=beta_t, r_squared=r2)


def classify_alpha_beta(reg: Regression, sv_factor: str = "short_vol",
                        alpha_t_min: float = 2.0) -> str:
    """Human verdict. If short-vol beta ≈ 1 and alpha is not significant, the
    strategy is replicable premium the book pays costs to reproduce."""
    b_sv = reg.betas.get(sv_factor)
    a_sig = reg.alpha_t is not None and abs(reg.alpha_t) >= alpha_t_min
    if b_sv is not None and 0.6 <= b_sv <= 1.4 and not a_sig:
        return "REPLICABLE_SHORT_VOL_BETA"          # the census's worst case, confirmed
    if a_sig and reg.alpha > 0:
        return "HAS_ALPHA"                           # genuine edge beyond the factor
    if a_sig and reg.alpha < 0:
        return "NEGATIVE_ALPHA"                      # worse than the factor after costs
    return "INCONCLUSIVE"                            # thin/insignificant


# ---- short-vol benchmark from the bhavcopy store ---------------------------

def _atm(strikes: Sequence[float], spot: float) -> Optional[float]:
    return min(strikes, key=lambda k: abs(k - spot)) if strikes else None


def short_vol_benchmark(
    store: Any, start: str, end: str, *, underlying: str = "NIFTY", hold_days: int = 1,
) -> List[Dict[str, Any]]:
    """Daily returns of selling the ATM straddle and holding `hold_days`.

    Return per day = (premium_collected − cost_to_close) / premium_collected, so it
    is a unit-free factor comparable across time. Uses nearest expiry with at least
    `hold_days` to run. Skips days with missing chain data (fail-closed, never
    fabricates a mark). Returns a list of {date, ret, premium} in date order.
    """
    out: List[Dict[str, Any]] = []
    bars = {b["date"][:10]: b for b in store.underlying_daily(underlying, start, end)}
    days = store.trading_days(start, end)
    for i, day in enumerate(days):
        if i + hold_days >= len(days):
            break
        exit_day = days[i + hold_days]
        bar = bars.get(day)
        if not bar:
            continue
        spot = float(bar["close"])
        chain = store.option_chain(underlying, day)
        # nearest expiry strictly after the exit day (avoid expiry settlement noise)
        exps = sorted(e for e in chain if e > exit_day)
        if not exps:
            continue
        exp = exps[0]
        strikes = list(chain.get(exp, {}).keys())
        k = _atm(strikes, spot)
        if k is None:
            continue
        ce0 = store.leg_settle(underlying, day, exp, k, "CE")
        pe0 = store.leg_settle(underlying, day, exp, k, "PE")
        ce1 = store.leg_settle(underlying, exit_day, exp, k, "CE")
        pe1 = store.leg_settle(underlying, exit_day, exp, k, "PE")
        if None in (ce0, pe0, ce1, pe1):
            continue
        premium = ce0 + pe0
        if premium <= 0:
            continue
        pnl = premium - (ce1 + pe1)               # short: collect, buy back
        out.append({"date": day, "ret": pnl / premium, "premium": premium})
    return out


def align_series(*series: List[Dict[str, Any]]) -> Tuple[List[str], List[List[float]]]:
    """Inner-join several [{date, ret}] lists on date. Returns (dates, [col,...])."""
    if not series:
        return [], []
    common = set.intersection(*[{r["date"] for r in s} for s in series]) if series else set()
    dates = sorted(common)
    cols = [[next(r["ret"] for r in s if r["date"] == d) for d in dates] for s in series]
    return dates, cols
