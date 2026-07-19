# IV Surface Richness

Core idea: ATM IV level alone is incomplete; richness relative to recent history, skew, and term structure matters.

QuantG use: P2 `iv_surface.py` feeds seller gates, contract scoring, and slow-premium validation.

Research rule: premium entries should include surface richness state.

Kill criterion: reject seller sleeves that trade when surface richness is unavailable or cheap unless separately justified.
