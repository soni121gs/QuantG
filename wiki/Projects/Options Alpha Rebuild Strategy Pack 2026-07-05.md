---
title: Options Alpha Rebuild Strategy Pack 2026-07-05
topic: Options Alpha Rebuild
status: seeded-paper-pack
created: 2026-07-05
---

# Options Alpha Rebuild Strategy Pack 2026-07-05

## Decision

Founder direction on 2026-07-05 superseded the earlier conservative recommendation: seed all 10 candidates as active paper templates for the next live market session, while keeping real live trading disabled.

The old book failed OOS, and the current research still says durable edge is most likely in defined-risk volatility selling, not generic intraday option buying. Therefore `QG-O1`..`QG-O10` are seeded for paper observation and evidence collection, not live approval. Intraday option-buying ideas remain unproven hypotheses until QuantG has 1-minute option history and they pass the same judge.

Seeded implementation: `backend/server.py` creates all 10 templates with `initial_status="live"`, startup enforces `mode="paper"`, and only app-supported structures are used: `credit_spread`, `debit_spread`, and `single_leg`.

## External Research Summary

- Index options are useful for both speculation and risk management, but outright call/put buying is a leveraged directional bet and must be tightly risk-limited. Source: Investopedia index options overview, https://www.investopedia.com/ask/answers/040815/what-index-option-trading-and-how-does-it-work.asp
- Option strategies should be backtested before systematic trading; this matches QuantG's OOS-first rule. Source: QuantInsti/Quantra options backtesting note, https://quantra.quantinsti.com/glossary/How-do-you-test-the-performance-of-an-options-trading-strategy-You-Backtest-it
- Volatility-risk-premium research supports the idea that index option implied volatility can exceed realized volatility, which is the economic reason defined-risk short-vol can work. Sources: SSRN Nifty VRP paper listing, https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6530119 and Barclays VRP note, https://indices.cib.barclays/dms/Public%20marketing/Volatility_Risk_Premium.pdf
- VWAP, ORB, Supertrend, and trend filters are common intraday frameworks, but public articles are not proof of edge. They are only idea sources. Source examples: ORB/VWAP explainer, https://www.sahi.com/blogs/orb-trading-strategy-explained and Supertrend indicator docs, https://docs.algotest.in/signals/pinescripts/supertrend_strategy/

## QuantG Validation Results

Run on VPS inside `quantg-backend` against `/app/data/bhavcopy_fo` using `core/eod_options_backtest.py`. This engine uses daily bhavcopy and validates held-to-theta structures; it does not prove intraday scalpers.

| Candidate | Structure | Underlying | OOS Verdict | n | Expectancy | OOS Exp | Note |
|---|---:|---:|---:|---:|---:|---:|---|
| QG-O1 NIFTY 3% OTM Weekly Put Spread Income | credit_spread | NIFTY | INSUFFICIENT_DATA | 23 | +287.3 | +563.7 | Promising; aligns with EDR-09 broader result. |
| QG-O2 NIFTY Trend-Filtered Put Spread Income | credit_spread | NIFTY | INSUFFICIENT_DATA | 16 | +95.6 | +508.4 | Promising but too few trades. |
| QG-O3 BANKNIFTY 3.5% OTM Weekly Put Spread Income | credit_spread | BANKNIFTY | INSUFFICIENT_DATA | 23 | -449.2 | -426.5 | Reject for now. |
| QG-O4 BANKNIFTY Range Iron Condor | iron_condor | BANKNIFTY | INSUFFICIENT_DATA | 22 | -767.1 | -1013.1 | Reject for now. |
| QG-O5 SENSEX 2% OTM Iron Condor | iron_condor | SENSEX | INSUFFICIENT_DATA | 17 | +400.1 | +654.8 | Promising; needs better sample construction. |
| QG-O6 SENSEX Range Iron Condor | iron_condor | SENSEX | INSUFFICIENT_DATA | 16 | +27.2 | +381.2 | Weak/fragile. |
| QG-O7 NIFTY Breakout Debit Call Spread | debit_spread | NIFTY | INSUFFICIENT_DATA | 28 | -843.4 | -1238.7 | Reject under EOD hold; intraday-only hypothesis. |
| QG-O8 BANKNIFTY Breakdown Debit Put Spread | debit_spread | BANKNIFTY | INSUFFICIENT_DATA | 11 | -1066.1 | -1222.1 | Reject under EOD hold; intraday-only hypothesis. |
| QG-O9 NIFTY Crash Long Put | single_leg | NIFTY | INSUFFICIENT_DATA | 24 | -2773.0 | -6166.5 | Reject under EOD hold; use only as tail-event paper hypothesis. |
| QG-O10 SENSEX Breakout Debit Call Spread | debit_spread | SENSEX | INSUFFICIENT_DATA | 26 | -31236.6 | -34780.9 | Reject. |

## Capital Rule For Retail Start

Starting assumption: paper wallet INR 500,000, but design for small retail risk.

- One live pilot at a time until 30 forward-paper trades.
- Strategy-level max loss per trade: target INR 2,500-6,000 for buyers, INR 8,000-22,000 for defined-risk spreads depending on width and index.
- Daily total book loss limit: 1.0%-1.5% of starting capital during paper-forward; do not let multiple strategies stack the same directional exposure.
- Weekly loss stop: 3% of capital; pause all new entries and review attribution.
- No naked option selling.
- No live promotion until OOS plus forward-paper both agree.

## The 10-Strategy Replacement Pack

### QG-O1 - NIFTY Put Spread Theta Core

- Role: primary income engine.
- Underlying: NIFTY.
- Structure: `credit_spread`.
- Direction: sell 3% OTM put spread.
- Entry: first valid weekly entry with 2-8 DTE.
- Exit: `exit_mode=expiry`; no intraday credit stop.
- App params: `short_otm_pct=0.03`, `wing_width=6`, `spread_width=6`, `short_delta~0.12`, `required_capital=50000-75000`.
- Status: seeded active paper template (`initial_status=live`, forced `mode=paper`).
- Why: best-known QuantG family from EDR-09; defined risk; uses existing 2-leg engine.

### QG-O2 - NIFTY Trend-Filtered Put Spread Theta

- Role: lower-frequency version of QG-O1.
- Underlying: NIFTY.
- Structure: `credit_spread`.
- Direction: sell 3% OTM put spread only when close > MA20 > MA50.
- Exit: hold to expiry.
- App params: same as QG-O1, `required_capital=25000`, `max_trades_day=1`, cooldown 60 minutes in the live template.
- Status: seeded active paper template; too few OOS trades in compact pass.
- Why: avoids selling downside insurance in weak trend regimes.

### QG-O3 - SENSEX Defined-Risk Short Vol Core

- Role: non-NIFTY income diversifier.
- Underlying: SENSEX.
- Structure: `credit_spread` in the seeded app template.
- Direction: sell 2% OTM defined-risk put spread.
- Entry: weekly 2-8 DTE.
- Exit: hold to expiry.
- App params: `short_otm_pct=0.02`, `wing_width=4`, `required_capital=30000`.
- Status: seeded active paper template as a 2-leg substitute for the researched condor family.
- Why: SENSEX short-vol was the only non-NIFTY family worth paper observation, but the live app should not seed unsupported 4-leg condors yet.

### QG-O4 - SENSEX Call Spread Range Pilot

- Role: lower-volatility filtered SENSEX short-vol.
- Underlying: SENSEX.
- Structure: `credit_spread` in the seeded app template.
- Entry: only when 10-day efficiency ratio < 0.35.
- Exit: hold to expiry.
- App params: `short_otm_pct=0.02`, `wing_width=4`, `required_capital=30000`.
- Status: seeded active paper template as a 2-leg range pilot.
- Why: designed to avoid trend weeks, but must prove cross-year consistency.

### QG-O5 - NIFTY Opening Range Call Buyer

- Role: intraday upside momentum option buyer.
- Underlying: NIFTY.
- Structure: `single_leg` or `debit_spread`; prefer `debit_spread` for defined loss.
- Entry: after 09:35 IST, buy CE only if 15-minute opening range high breaks, price is above VWAP, 5-minute candle closes strong, and VIX/ATR expansion confirms movement.
- Exit: same-day only; 0.7R stop, 1.4R target, trail after 1R, force squareoff by 14:45.
- Capital: one lot only; max premium risk INR 2,500-4,000.
- Status: paper-only until 1-minute option data validates it.
- Why: option buying needs sharp directional movement; ORB plus VWAP filters stop random theta bleed.

### QG-O6 - NIFTY Opening Range Put Buyer

- Role: intraday downside momentum option buyer.
- Underlying: NIFTY.
- Structure: `single_leg` or `debit_spread`; prefer `debit_spread`.
- Entry: after 09:35 IST, buy PE only if opening range low breaks, price below VWAP, breadth/market regime bearish, and the candle closes near low.
- Exit: same as QG-O5.
- Capital: one lot only; max premium risk INR 2,500-4,000.
- Status: paper-only until intraday validation.
- Why: symmetric counterpart to QG-O5; should be disabled in flat/range regimes.

### QG-O7 - BANKNIFTY VWAP Reclaim Call Buyer

- Role: high-beta intraday buyer, used sparingly.
- Underlying: BANKNIFTY.
- Structure: `debit_spread`.
- Entry: first reclaim of VWAP after a failed downside move, higher low, ADX rising, and range expansion.
- Exit: 0.6R stop, 1.2R target, max hold 25 minutes.
- Capital: one lot; max loss INR 3,000-5,000.
- Status: paper-only; BANKNIFTY EOD tests were negative.
- Why: BANKNIFTY is noisy; only trade after failed breakdown plus VWAP reclaim, not generic momentum.

### QG-O8 - BANKNIFTY VWAP Reject Put Buyer

- Role: high-beta downside intraday buyer.
- Underlying: BANKNIFTY.
- Structure: `debit_spread`.
- Entry: price rejects VWAP from below, lower high forms, opening range low or prior swing low breaks.
- Exit: 0.6R stop, 1.2R target, max hold 25 minutes.
- Capital: one lot; max loss INR 3,000-5,000.
- Status: paper-only.
- Why: designed for fast trend days only; otherwise disabled.

### QG-O9 - NIFTY Tail Event Put Buyer

- Role: rare crash-day hedge/speculation.
- Underlying: NIFTY.
- Structure: `single_leg` PE or put debit spread.
- Entry: 3-bar underlying move < -1.8%, India VIX rising, price below VWAP, no late-afternoon entries.
- Exit: same-day; stop quickly if rebound above VWAP.
- Capital: smallest allocation; max loss INR 2,000-3,000.
- Status: paper-only. EOD hold failed badly, so this must be intraday only.
- Why: not a routine strategy; only for volatility expansion days.

### QG-O10 - NIFTY Intraday Premium-Safe Debit Spread Buyer

- Role: lower-premium directional option buyer.
- Underlying: NIFTY.
- Structure: `debit_spread`.
- Entry: same triggers as QG-O5/QG-O6, but only when ATM premium is expensive relative to 5-day realized range.
- Exit: 0.7R stop, 1.3R target, max hold 35 minutes.
- Capital: max loss limited by debit; one lot only.
- Status: paper-only until minute data.
- Why: if the founder wants option buying, debit spreads are the retail-safe version because paid premium and theta are capped.

## What Not To Build

- No naked short straddles or strangles.
- No BANKNIFTY income strategies until a new OOS sweep proves them; compact pass was negative.
- No SENSEX debit-spread buyers; compact pass was deeply negative.
- No generic scalpers that trade every day.
- No Martingale, averaging down, or "recover loss" sizing.

## Required App Work After Seeding The Full Pack

1. Refresh Edge Lab snapshot after CM/FO data changes.
2. Fix stale tests that assume all seeded strategies use `signal_or_tp_sl_trailing`.
3. Compare QG-O1..QG-O10 after each paper session by signal count, skip reason, fill count, gross/net P&L, MFE/MAE, and max drawdown.
4. Add an intraday options data store before promoting QG-O5 to QG-O10.
5. Add a per-strategy `strategy_family` tag: `income`, `intraday_buyer`, `tail_event`.
6. Add hard portfolio caps: max one income position and max one buyer position open per underlying.
7. Add UI badges: OOS passed, forward-papering, intraday-unvalidated.

## Promotion Ladder

1. Backtest/OOS pass.
2. Paper-forward 30 trades minimum.
3. Positive expectancy after costs.
4. Max drawdown acceptable under planned capital.
5. Manual founder approval.
6. Live pilot at one lot / one strategy only.

## Intraday Judge Built (IMD pipeline, 2026-07-06)

The full **1-minute intraday OOS pipeline** is now implemented (IMD-01..IMD-10 — see CLAUDE.md §14). It is the dedicated judge for the intraday BUYERS `QG-O5`..`QG-O10`, which the EOD bhavcopy engine cannot fairly evaluate. It is JUDGE-FIRST: verdicts stay `INSUFFICIENT_DATA` until (a) real Upstox 1-minute option history is imported and (b) underlying index 1-minute candles are supplied.

**Final intraday promotion ladder (the law for `QG-O5`..`QG-O10`):**
1. Import real 1-minute option history (`scripts/options_1m_ingest_upstox.py`) + underlying index minutes.
2. Pass `scripts/run_intraday_options_validation.py` — the `GATE`: **≥30 trades, ≥3 months, ≤20% missing-minute rate, OOS expectancy > 0 after costs, ≥50% green months.**
3. Forward-paper 3–6 weeks; live paper P&L must track the OOS expectancy.
4. Manual founder approval; `CORE_ENGINE_LIVE_ENABLED` stays false by default.
5. Live pilot at one lot / one strategy only.

Do NOT tune QG-O5..QG-O10 thresholds from one paper day. Paper P&L alone never proves an edge — only a passing intraday OOS verdict + forward-paper does. See [[Intraday Minute Data Pipeline 07-06]].

## Current Recommendation

Run the seeded `QG-O1`..`QG-O10` pack in paper for the next live market session, then review evidence before changing thresholds. Prioritize QG-O1/QG-O2 first if multiple strategies fire, and treat QG-O5..QG-O10 as data-gathering intraday buyer hypotheses until the intraday OOS judge has real 1-minute data.
