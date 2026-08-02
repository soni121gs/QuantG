"""P5-M5 (2026-08-02): information coefficient (IC) of the scores QuantG computes
but never checked — `contract_edge_score` (dynamic_contract_selector, §16.4), the
RAE regime confidence (§18), and EdgeMath conviction (§16).

IC = rank correlation between a score assigned BEFORE a trade and that trade's
realized forward P&L. It is the "IC" in Grinold-Kahn's IR = IC·√BR. An IC
indistinguishable from zero means the score has no predictive content — the
machinery that computes it is DECORATION, and any sizing that leans on it is
sizing on noise. This is the deterministic screen; it never trades or mutates.

Pure: Spearman + verdict are I/O-free; the CLI (`scripts/run_score_ic.py`) pulls
the (score, realized_pnl) pairs from db.strategy_positions on the VPS.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _rank(xs: Sequence[float]) -> List[float]:
    """Fractional ranks (ties share the average rank)."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / (va ** 0.5 * vb ** 0.5)


@dataclass
class ICResult:
    name: str
    n: int
    ic: Optional[float]              # Spearman rank correlation
    t_stat: Optional[float]
    verdict: str

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "n": self.n,
                "ic": (round(self.ic, 4) if self.ic is not None else None),
                "t_stat": (round(self.t_stat, 2) if self.t_stat is not None else None),
                "verdict": self.verdict}


def information_coefficient(name: str, pairs: Sequence[Tuple[float, float]],
                            *, t_min: float = 2.0, ic_min: float = 0.03) -> ICResult:
    """Spearman IC of score vs forward P&L. Verdicts:
      DECORATION           — n large enough but IC insignificant (t below t_min).
      PREDICTIVE           — significant IC, correct sign (higher score → higher P&L).
      INVERTED             — significant IC but NEGATIVE (the score is backwards!).
      INSUFFICIENT_DATA    — fewer than 20 pairs.
    """
    clean = [(float(s), float(p)) for s, p in pairs
             if s is not None and p is not None]
    n = len(clean)
    if n < 20:
        return ICResult(name, n, None, None, "INSUFFICIENT_DATA")
    scores = _rank([s for s, _ in clean])
    pnls = _rank([p for _, p in clean])
    ic = _pearson(scores, pnls)
    if ic is None:
        return ICResult(name, n, None, None, "INSUFFICIENT_DATA")
    t = None
    if abs(ic) < 1.0:
        t = ic * ((n - 2) / (1 - ic * ic)) ** 0.5
    significant = t is not None and abs(t) >= t_min and abs(ic) >= ic_min
    if not significant:
        verdict = "DECORATION"
    elif ic > 0:
        verdict = "PREDICTIVE"
    else:
        verdict = "INVERTED"
    return ICResult(name, n, ic, t, verdict)
