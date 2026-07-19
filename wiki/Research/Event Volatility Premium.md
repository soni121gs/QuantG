# Event Volatility Premium

Core idea: earnings and event options can overprice realized moves, but the tail and physical settlement risks are severe.

QuantG use: P3-1 skips event-expiry and expiry-week stock-option entries, enters T-1, exits T+1, and uses defined-risk structures.

Research rule: event-vol hypotheses must explicitly handle settlement, liquidity, and gap risk.

Kill criterion: reject event-premium ideas without defined max loss and event-count breadth.
