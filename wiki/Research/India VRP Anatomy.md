# India VRP Anatomy

Core idea: volatility risk premium can exist in Indian index and stock options, but it is regime-dependent and cost-sensitive.

QuantG use: P2 IV surface and P3 premium validators test whether volatility is rich enough to sell.

Research rule: VRP claims require IV/RV richness, surface richness, and realized-vol follow-through evidence.

Kill criterion: reject short-vol entries when VIX/ATM IV is cheap or realized volatility dominates implied.
