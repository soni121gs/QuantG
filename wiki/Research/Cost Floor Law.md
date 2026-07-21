# Cost Floor Law

Core idea: expected edge must exceed modeled trading friction by a large margin before paper or live.

QuantG use: ERP requires expected edge >= 3x modeled friction. Enforced at BUILD time in `core/spread_builder.credit_cost_floor` (2026-07-21), not only in research validators — the live path previously opened trades the Diagnostician had already condemned.

Research rule: every hypothesis card must state friction and expected edge multiple.

Kill criterion: reject low-credit, high-turnover options trades unless edge clears the cost floor.

## Measured proof (2026-07-21, live Upstox chain during market hours)

Probed 3 underlyings x 3 expiries x 6 widths x 5 deltas.

- `short_delta` 0.12 clears the 3x floor in **ZERO** geometries. NIFTY w6 multiple 2.05, BANKNIFTY w6 2.18, SENSEX w6 1.27.
- Width-1 spreads (the QG-O11 scalp) clear in **ZERO** cases at any delta or expiry. Best case 1.74; 0.14-0.40 at 0 DTE.
- On a 0-DTE expiry afternoon, **none of 60** geometries clears. The chain simply does not pay enough to transact.
- At `short_delta` 0.30 the same widths clear at 3.0-6.4x.

Round-trip friction is ~Rs300/lot (slippage on 4 legs + brokerage + taxes) — the figure `dynamic_exit.TRAIL_MIN_ARM_RUPEES` already encoded. An earlier probe constant of Rs85 understated it 3.5x, which is how sub-floor geometry passed `static.cost_floor`.

Smaller lot sizes sit closer to the floor: SENSEX (lot 20) needs a wider wing than NIFTY (lot 65) to collect the same rupee credit.

Practical tell: look at the long wing's price. QG-O1's was Rs0.46 while consuming Rs32,000/lot of risk budget — a naked short with a lottery ticket attached. A nearly worthless wing means the structure is not meaningfully defined-risk.

See [[Theta Reachability Law]]. Both laws must hold together: the cost floor says the prize is worth collecting, reachability says you can actually collect it.
