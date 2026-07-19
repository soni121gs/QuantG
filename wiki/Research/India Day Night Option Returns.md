# India Day Night Option Returns

Core idea: intraday and overnight returns can have different drivers and should not be pooled blindly.

QuantG use: P3-2 validates close-to-close momentum separately from overnight drift.

Research rule: split intraday, overnight, and multi-day holds in probes.

Kill criterion: reject effects that reverse sign when overnight and session components are separated.
