from __future__ import annotations

import math
import random
from dataclasses import dataclass, asdict
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Sequence


@dataclass(frozen=True)
class ResearchMetrics:
    sample_n: int
    total_pnl: float
    expectancy: float
    win_rate: float
    avg_win: float
    avg_loss: float
    payoff_ratio: float
    profit_factor: float
    max_drawdown: float
    cvar_95: float
    sharpe_like: float
    fractional_kelly: float
    bayesian_win_rate_mean: float
    bootstrap_expectancy_low: float
    bootstrap_expectancy_high: float
    monte_carlo_worst_drawdown_p95: float
    slippage_stress: Dict[str, Any]
    multiple_testing_penalty: float
    verdict: str
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _clean(values: Iterable[float]) -> List[float]:
    return [float(v) for v in values if v is not None and math.isfinite(float(v))]


def expectancy(pnls: Sequence[float]) -> float:
    values = _clean(pnls)
    return mean(values) if values else 0.0


def max_drawdown(pnls: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    worst = 0.0
    for pnl in _clean(pnls):
        equity += pnl
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)


def profit_factor(pnls: Sequence[float]) -> float:
    values = _clean(pnls)
    gross_profit = sum(v for v in values if v > 0)
    gross_loss = abs(sum(v for v in values if v < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def cvar(pnls: Sequence[float], alpha: float = 0.95) -> float:
    values = sorted(_clean(pnls))
    if not values:
        return 0.0
    tail_count = max(1, int(math.ceil(len(values) * (1 - alpha))))
    return abs(mean(values[:tail_count]))


def bayesian_win_rate(wins: int, losses: int, prior_alpha: float = 1.0, prior_beta: float = 1.0) -> float:
    return (wins + prior_alpha) / (wins + losses + prior_alpha + prior_beta)


def fractional_kelly(win_rate: float, avg_win: float, avg_loss: float, fraction: float = 0.25) -> float:
    if avg_win <= 0 or avg_loss <= 0:
        return 0.0
    b = avg_win / avg_loss
    full_kelly = (win_rate * b - (1 - win_rate)) / b
    return max(0.0, min(1.0, full_kelly * fraction))


def bootstrap_expectancy_ci(
    pnls: Sequence[float],
    *,
    iterations: int = 500,
    alpha: float = 0.10,
    seed: int = 42,
) -> Dict[str, float]:
    values = _clean(pnls)
    if not values:
        return {"low": 0.0, "high": 0.0}
    rng = random.Random(seed)
    estimates = []
    for _ in range(max(10, iterations)):
        sample = [values[rng.randrange(len(values))] for _ in values]
        estimates.append(mean(sample))
    estimates.sort()
    low_idx = max(0, int((alpha / 2) * len(estimates)) - 1)
    high_idx = min(len(estimates) - 1, int((1 - alpha / 2) * len(estimates)) - 1)
    return {"low": estimates[low_idx], "high": estimates[high_idx]}


def monte_carlo_drawdown(
    pnls: Sequence[float],
    *,
    paths: int = 300,
    seed: int = 7,
    quantile: float = 0.95,
) -> float:
    values = _clean(pnls)
    if not values:
        return 0.0
    rng = random.Random(seed)
    drawdowns = []
    for _ in range(max(10, paths)):
        path = [values[rng.randrange(len(values))] for _ in values]
        drawdowns.append(max_drawdown(path))
    drawdowns.sort()
    idx = min(len(drawdowns) - 1, max(0, int(math.ceil(quantile * len(drawdowns))) - 1))
    return drawdowns[idx]


def slippage_stress(pnls: Sequence[float], per_trade_costs: Sequence[float]) -> Dict[str, Any]:
    values = _clean(pnls)
    costs = _clean(per_trade_costs)
    stressed = {}
    for cost in costs:
        adjusted = [v - cost for v in values]
        stressed[str(cost)] = {
            "expectancy": expectancy(adjusted),
            "total_pnl": sum(adjusted),
            "profit_factor": profit_factor(adjusted),
            "passes": expectancy(adjusted) > 0,
        }
    return stressed


def multiple_testing_penalty(trials_tested: int) -> float:
    if trials_tested <= 1:
        return 0.0
    return min(0.75, math.log10(trials_tested) / 4.0)


def summarize_research_metrics(
    pnls: Sequence[float],
    *,
    r_multiples: Sequence[float] | None = None,
    per_trade_costs: Sequence[float] = (0.0, 25.0, 50.0, 100.0),
    trials_tested: int = 1,
    min_sample: int = 30,
) -> Dict[str, Any]:
    values = _clean(pnls)
    sample_n = len(values)
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    win_rate = len(wins) / sample_n if sample_n else 0.0
    avg_win = mean(wins) if wins else 0.0
    avg_loss = abs(mean(losses)) if losses else 0.0
    exp = expectancy(values)
    pf = profit_factor(values)
    sd = pstdev(values) if sample_n > 1 else 0.0
    sharpe_like = (exp / sd * math.sqrt(sample_n)) if sd > 0 else 0.0
    ci = bootstrap_expectancy_ci(values)
    stress = slippage_stress(values, per_trade_costs)
    penalty = multiple_testing_penalty(trials_tested)
    adjusted_expectancy = exp * (1 - penalty)
    warnings: List[str] = []

    if sample_n < min_sample:
        warnings.append(f"sample below gate: {sample_n} < {min_sample}")
    if ci["low"] <= 0 < ci["high"]:
        warnings.append("bootstrap interval crosses zero")
    if any(not row["passes"] for row in stress.values()):
        warnings.append("edge fails at one or more slippage stress levels")
    if penalty > 0:
        warnings.append(f"multiple-testing penalty applied for {trials_tested} trials")
    if win_rate > 0.7 and exp <= 0:
        warnings.append("high win-rate but non-positive expectancy")

    if sample_n < min_sample:
        verdict = "INSUFFICIENT_DATA"
    elif adjusted_expectancy <= 0:
        verdict = "NO_EDGE_NEGATIVE"
    elif warnings:
        verdict = "FRAGILE"
    else:
        verdict = "CANDIDATE_EDGE"

    metrics = ResearchMetrics(
        sample_n=sample_n,
        total_pnl=sum(values),
        expectancy=exp,
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=(avg_win / avg_loss) if avg_loss > 0 else float("inf") if avg_win > 0 else 0.0,
        profit_factor=pf,
        max_drawdown=max_drawdown(values),
        cvar_95=cvar(values, 0.95),
        sharpe_like=sharpe_like,
        fractional_kelly=fractional_kelly(win_rate, avg_win, avg_loss),
        bayesian_win_rate_mean=bayesian_win_rate(len(wins), len(losses)),
        bootstrap_expectancy_low=ci["low"],
        bootstrap_expectancy_high=ci["high"],
        monte_carlo_worst_drawdown_p95=monte_carlo_drawdown(values),
        slippage_stress=stress,
        multiple_testing_penalty=penalty,
        verdict=verdict,
        warnings=warnings,
    )
    result = metrics.to_dict()
    if r_multiples is not None:
        clean_r = _clean(r_multiples)
        result["r_multiple_expectancy"] = expectancy(clean_r)
    return result
