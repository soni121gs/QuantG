---
claim_type: measured
verified: 2026-07-25
reproduction: CLAUDE.md sections 13, 20, 21, 22
---

# QuantG Structural Trading Rules

## OOS First

Every strategy follows: hypothesis -> out-of-sample validation -> forward paper -> founder-gated live.

Paper P&L alone is not proof. A strategy needs enough sample, positive OOS expectancy, cost-floor clearance, and risk-adjusted significance before promotion.

## Cost Floor

Expected bankable edge must be at least 3x modeled round-trip friction before a design can be promoted.

Credit-spread checks must use bankable profit, not gross credit. For TP-based sellers, bankable profit is `credit_tp_frac * credit * lot_size`.

## Theta Reachability

Intraday theta sellers must have a hold window and DTE where decay can plausibly deliver most of the take-profit.

Formula: `theta_reachable_frac = hold_minutes / (dte_days * 375)`, `ratio = theta_reachable_frac / credit_tp_frac`.

## Breadth

Prefer independent bets across events, names, and regimes over many parameterizations of one NIFTY short-vol bet.

## Overfitting

Every verdict must carry trials count and a multiple-testing-aware risk adjustment. Positive expectancy without significance is fragile, not an edge.

## Live Safety

`CORE_ENGINE_LIVE_ENABLED` stays false unless the founder explicitly arms the live ladder. Hermes may observe and recommend; it must not trade or edit code.
