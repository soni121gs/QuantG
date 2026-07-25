---
claim_type: aspiration
verified: 2026-07-25
reproduction: review source note and linked QuantG code or task evidence
---

# Lopez de Prado Deflated Sharpe

Core idea: Sharpe ratios must be deflated for non-normality, sample size, and the number of trials.

QuantG use: ERL carries explicit `trials_count` and a DSR proxy so candidate edges can be downgraded for overfit risk.

Research rule: every sweep must report the number of tried configurations.

Kill criterion: reject edges that are only positive before multiple-testing adjustment.
