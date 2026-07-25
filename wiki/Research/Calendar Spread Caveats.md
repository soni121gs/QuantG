---
claim_type: literature
verified: 2026-07-25
reproduction: review source note and linked QuantG code or task evidence
---

# Calendar Spread Caveats

Core idea: calendar spreads depend on term structure, realized move, and near/far expiry liquidity.

QuantG use: P2-5 added research-only calendar-spread pricing; there is no live calendar execution path.

Research rule: calendars must be judged as research structures before any execution design.

Kill criterion: reject calendars that look positive only because far-leg marks are stale or near-expiry settlement is mishandled.
