# Regime First Options

Core idea: options edge depends heavily on market regime: trend, range, chop, event, and volatility state.

QuantG use: RAE and Phase 3 validators separate trend buyers, range sellers, event sleeves, and overlays.

Research rule: every sleeve must name the regime it wants and the regime it avoids.

Kill criterion: reject strategies that fire across all regimes without evidence.
