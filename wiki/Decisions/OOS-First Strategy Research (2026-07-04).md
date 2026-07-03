---
topic: OOS-First Strategy Research
tags: [strategy, backtesting, edge, discipline, hermes]
date: 2026-07-04
---

# Decision: OOS-First Strategy Research (2026-07-04)

## Context
For months QuantG could not backtest its option strategies — Upstox 404s on
expired-option history. So strategies were tuned on live paper P&L, which at
~13 trades/day is mostly noise. The result was an endless "notice a red day →
tweak a strategy → deploy → hope" treadmill with no proven profitability.

## What changed
The **data wall is broken**. The free NSE F&O bhavcopy (UDiFF, EOD) gives real
per-contract settlement prices. Two years are ingested (494 days) and an
**out-of-sample (OOS) backtester** now grades any option strategy — see
[[Hermes Agent Integration Roadmap]] and CLAUDE.md §13.

## The verdict
The OOS validator graded the whole book: **0 of 11 option strategies have an
out-of-sample edge.** A 72-configuration sweep of exit-geometry / width / expiry
found **0 configurations** that cross positive OOS. The credit-spread exit
geometry (risk 100% of credit to make 50%) is structurally negative. This
matches the live book's ~−₹86/trade. The apparent "winning theta cluster" was
small-sample illusion.

## The decision (now governing law)
1. **Stop tuning the existing strategies.** They have no edge; tuning is the treadmill.
2. **Every strategy change or new strategy MUST pass the OOS validator first.**
   Discipline: `hypothesis → OOS backtest → forward-paper (3–6 wks) → live pilot`.
3. **Grade IDEAS on out-of-sample expectancy, not daily paper P&L.** Nothing is
   "working" until it has 30+ trades AND is positive out-of-sample.
4. **Archive the dead strategies** (11 options + 10 equity) and rebuild from
   base-rate studies on the data — not from intuition.

## For Hermes
Hermes stays read-only and narrates; it does not trade or edit code. When Hermes
reports on strategy performance, it should frame results against this discipline:
a strategy is not validated by a good day — only by surviving out-of-sample. The
OOS verdict (0/11) is the current baseline truth. This decision is complementary
to the HSI Stage-4 live-attribution OOS judge (`core/hermes_validator.py`); both
enforce "out-of-sample or it doesn't count."

## Known gaps
- SENSEX/BANKEX (BSE) history is Akamai-gated and not yet fetched.
- Equity strategies cannot be backtested (index-only data) — need NSE_EQ stock EOD data.
