# Theta Reachability Law

Core idea: a premium seller's take-profit must be reachable by TIME DECAY inside its own hold window. If it is not, the trade is a directional coin flip wearing a theta costume, and the exit is decided by whichever clock fires first.

QuantG use: `core/spread_builder.tp_reachability(tp_frac, dte_days, hold_minutes)`. Diagnostician probe `static.tp_reachability` fires when the ratio drops below 0.55.

Research rule: state `dte_days`, `hold_minutes` and `tp_frac` on every seller card, and report the reachability ratio alongside the cost-floor multiple.

Kill criterion: reject a seller whose theta supplies less than ~55% of its take-profit target inside the hold. Lengthen the hold, shorten the DTE, or lower the TP — do not ship it hoping direction pays the difference.

## The formula

```
theta_reachable_frac = hold_minutes / (dte_days * 375)      # 375 market-minutes/day
ratio                = theta_reachable_frac / tp_frac
```

Linear approximation of extrinsic decay. Decay is convex and accelerates near expiry, so this is conservative at short DTE.

Read it as: ratio >= 1.0 decay alone reaches the target (a genuine harvest); ~0.6 theta does most of the work and direction is a tailwind; < 0.3 the target needs a directional gift.

## Measured proof (2026-07-21, QuantG seller book)

Of **71 closed trades, only 10 exited on a price trigger — 86% were closed by a clock.** The per-strategy reachability ratio rank-ordered both the price-exit rate AND the realized P&L:

| Strategy | Reachability | Price exits | P&L |
|---|---|---|---|
| QG-O4 | 0.32 | 26% | +Rs1,768 (only winner) |
| RAE SENSEX | 0.32 | 33% | -Rs2,981 |
| QG-O11 | 0.16 | 14% | -Rs8,152 |
| QG-O1 / RAE NIFTY / RAE BANKNIFTY | 0.09 | **0 of 24 trades** | -Rs16,461 |

QG-O1 held a ~7 DTE weekly for 120 minutes against a 0.50 TP: theta could supply 0.05 of credit, so 91% of the target had to arrive as a favourable move. Not one of its trades ever reached its own target or its own stop.

## Corollary: expiry cycle constrains strategy horizon

BANKNIFTY is **monthly-expiry only**, so its nearest DTE is 7-30 days. At a 300-minute hold and TP 0.45 that is a reachability of 0.25 (7 DTE) falling to 0.06 (30 DTE). **BANKNIFTY cannot be an intraday theta seller at any width or delta** — the fix is a multi-day hold, not a wider spread. Verified 2026-07: NIFTY weekly expiry Tuesday, SENSEX Thursday, BANKNIFTY monthly.

See [[Cost Floor Law]]. The two laws pull in opposite directions and must be solved together: the cost floor pushes toward nearer expiries and fatter credit, reachability pushes toward shorter DTE and longer holds — while 0-DTE fails the cost floor outright.
