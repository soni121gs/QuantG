---
title: Strategy Experiment Ledger
topic: Projects
tags: [research, strategy, ledger, backtest, baseline]
date: 2026-06-20
---

# Strategy Experiment Ledger (TASK-H017)

This ledger tracks all quantitative strategy hypotheses, versions, clean baseline epochs, results, decisions, and rationale on the QuantG platform.

## Research Baseline Epochs

* **Clean Epoch reset**: Established 2026-06-19 to define the first trustworthy forward-performance window after fixing the websocket tick feed (TASK-044) and theta entry-guards. All comparative metrics are calculated from this epoch.

---

## Strategy Experiments Ledger

| Date | Strategy/Hypothesis | Version/Setup | Baseline Date | Experiment Results & Metrics | Decision | Rationale & Comments |
|---|---|---|---|---|---|---|
| 2026-06-19 | Pause cumulative buyer losers | Nifty Quick EMA Scalper, Upstox Nifty ATM Momentum, Upstox Banknifty ATM Breakout | 2026-06-19 | Buyers are historically negative expectancy under low volatility / range conditions. | **PAUSED** | Prevent capital bleed on trending single-leg buys until out-of-sample edge is proven. |
| 2026-06-19 | Deploy Theta Credit Spreads | NIFTY Range Credit Spread, BANKNIFTY Theta Credit Spread, SENSEX Theta Credit Spread | 2026-06-19 | Target theta-positive mean-reversion during flat range days. | **LIVE (Paper)** | Initialized forward walk-forward testing. Scoring rank scheduled for 2026-06-26. |
| 2026-06-19 | Debit Spread Structure Support | Buy ATM + Sell OTM defined-risk layout | 2026-06-19 | Cuts cost basis ~40% and shields directional views from theta bleed. | **ENABLED** | Added full core lifecycles and paper fills. Converted one ATM buyer to debit spread for testing. |

---

## Guidelines for Proposing Experiments

When designing a new strategy experiment, Hermes or the operator should write a draft entry following these fields:
1. **Hypothesis**: The exact trading rule modification or setup change.
2. **Version / Parameter Settings**: Specific entry/exit triggers, SL/TP levels, delta/IV filters.
3. **Clean Baseline**: The date from which out-of-sample performance will be measured.
4. **Metrics**: Realized Profit Factor, Win-Rate, Sharpe, Sortino, and Expectancy.
5. **Decision**: Maintain, Promote to Live, Pause, or Retune.
