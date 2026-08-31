---
claim_type: measured
verified: 2026-08-31
reproduction: "Read-only VPS Mongo probe over db.strategy_positions since 2026-08-01T00:00:00Z plus db.strategies config snapshot."
---

# Strategy Profitability Review 2026-08-31

This report reviews the last 30 days of QuantG paper trading evidence and proposes strategy changes for higher realized profit with bounded loss. It does not claim any strategy is live-ready. All changes below must pass the QuantG ladder: hypothesis -> OOS/backtest where available -> forward-paper -> founder-gated live.

## Executive Verdict

The book is not losing because one monitor failed. The recurring pattern is:

- Credit sellers often win more than they lose by count, but losses are larger than wins.
- Many losing trades were profitable first, then gave back profit.
- Hold-to-expiry spreads improved after profit protection was deployed, but the default protection is still too loose for some structures.
- Single-leg/debit directional trades have too little sample and too much tail risk to scale.
- The best 30-day evidence is in two NIFTY hold-to-expiry/slow-premium spreads, not in the high-frequency SENSEX seller book.

## Last 30-Day Strategy Evidence

| Strategy | Closed | Open | P&L | Avg | WR | Profit Factor | Main Problem | Action |
|---|---:|---:|---:|---:|---:|---:|---|---|
| HTE NIFTY Defined-Risk Put Spread | 22 | 1 | +14965.44 | +680.25 | 40.9% | 31.18 | Large giveback, but tiny realized losses | Keep, tighten profit lock, cap size until OOS |
| QG-O1 NIFTY Put Spread Theta Core | 12 | 2 | +4793.78 | +399.48 | 75.0% | 1.60 | Three large losses dominate | Keep small, tighter loss/giveback guard |
| RAE SENSEX Trend Delta-1 | 4 | 0 | +718.63 | +179.66 | 75.0% | 7.85 | Sample too small | Observe only, no scaling |
| IDX NIFTY VRP Call-Spread | 31 | 1 | +673.92 | +21.74 | 64.5% | 1.16 | Edge barely above friction | Keep at minimum size, tighten filters |
| RAE BANKNIFTY Trend Delta-1 | 2 | 0 | -506.42 | -253.21 | 0.0% | n/a | Monthly expiry / theta decay | Pause |
| RAE NIFTY Range Seller | 24 | 0 | -1799.20 | -74.97 | 45.8% | 0.69 | Too many squareoff/no-progress exits | Pause or require richer IV and tighter profit lock |
| RAE SENSEX Range Seller | 39 | 0 | -2025.00 | -51.92 | 61.5% | 0.79 | High WR but negative expectancy | Pause; do not scale |
| Tail Hedge NIFTY Far-OTM Put Spread | 10 | 1 | -2168.68 | -216.87 | 40.0% | 0.87 | Big winner giveback / expiry bleed | Convert from HTE lottery to explicit profit-capture hedge |
| IDX SENSEX VRP Put-Spread | 99 | 0 | -6903.20 | -69.73 | 53.5% | 0.75 | Overtrading negative expectancy | Keep paused |
| IDX NIFTY Mean-Reversion Fade | 2 | 0 | -8225.37 | -4112.69 | 0.0% | n/a | One huge debit-spread stop | Kill or require fresh OOS |

No 30-day trades:

- QG-O4 SENSEX Call Spread Range Pilot: live status but no sample; diagnose signal/reachability before changing.
- QG-O11 NIFTY Regime Seller Credit Scalp: archived; do not wake unless revalidated at realistic friction.
- RAE BANKNIFTY Range Seller: archived; keep archived because BANKNIFTY monthly-only makes intraday theta reachability poor.
- RAE NIFTY Trend Delta-1: live status but no sample; diagnose whether gates are correctly standing down.
- IDX NIFTY Long-Gamma: live status but no sample; only trade in true HIGH_VOL_CHOP/event regimes.

## Recommended Book Changes

### 1. Reduce The Book To Evidence-Backed Sleeves

Run only these in paper-forward:

- HTE NIFTY Defined-Risk Put Spread: smallest size, because 30-day P&L is strongly positive.
- QG-O1 NIFTY Put Spread Theta Core: smallest size, because positive but sample is still thin and losses are large.
- IDX NIFTY VRP Call-Spread: observe-only or minimum size, because it is slightly positive but fragile.
- RAE SENSEX Trend Delta-1: observe-only, because sample is only 4 trades.

Pause or keep paused:

- IDX SENSEX VRP Put-Spread.
- RAE SENSEX Range Seller.
- RAE NIFTY Range Seller.
- RAE BANKNIFTY Trend Delta-1.
- IDX NIFTY Mean-Reversion Fade.
- Tail Hedge as currently configured.

### 2. Tighten Universal Profit Protection

Current protection arms at 15% of capacity, minimum Rs300, and exits after a 70% giveback. This is too forgiving. For paper-forward testing, compare:

- Conservative: arm at Rs300 or 10% capacity, exit after 35% giveback.
- Balanced: arm at Rs300 or 12% capacity, exit after 45% giveback.
- Current: arm at 15%, exit after 70% giveback.

The winner must be chosen by realized forward-paper plus replay/backtest where possible, not by preference.

### 3. Make Debit Spreads Pay Themselves First

Debit spreads need a different exit contract from credit spreads:

- Take partial/full profit when open P&L reaches 1.0x initial debit or a fixed rupee lock.
- Trail from peak with 35%-45% giveback, not 70%.
- Cut if it has not gone green within the expected impulse window.
- Never hold a debit spread to expiry unless the strategy is explicitly a disaster hedge with a pre-budgeted bleed.

This directly targets the Tail Hedge and NIFTY Mean-Reversion Fade failures.

### 4. Stop Trading High-WR Negative-Expectancy Sellers

SENSEX sellers are the clearest trap:

- IDX SENSEX Put-Spread: 99 closed, -Rs6903, WR 53.5%, PF 0.75.
- RAE SENSEX Range Seller: 39 closed, -Rs2025, WR 61.5%, PF 0.79.

These should not be tuned live. They need an OOS sweep over DTE, IV richness, entry time, spread width, and stop/target shape. Until then, keep them paused.

### 5. Add A Strategy Promotion/Demotion Governor

Every strategy should auto-demote to observe-only when any of these are true over the recent window:

- n >= 20 and profit factor < 1.05.
- n >= 20 and expectancy < modeled friction.
- green-then-loss rate among losers > 50%.
- one worst loss exceeds 4x average win.
- no trades for 30 days while status is live.

Promotion should require:

- n >= 30 forward-paper trades.
- Positive expectancy after friction.
- Profit factor > 1.2.
- No single loss wipes more than three average wins.
- OOS or replay support for the same structure.

### 6. Use AI For Research, Not Order Decisions

Hermes should generate weekly hypothesis cards from:

- Strategy-level realized P&L, win/loss ratio, giveback, exit reasons.
- Regime at entry, DTE, IV/RV richness, contract score, time-of-day.
- OOS/replay result with t-stat/HAC and trials count.

It should not place or exit trades. The useful AI loop is: find measurable patterns -> create testable strategy hypothesis -> run deterministic backtest/replay -> suggest a gated config change.

## Strategy-by-Strategy Actions

### HTE NIFTY Defined-Risk Put Spread

Keep paper-forward. It is the strongest recent result: +Rs14965 over 22 closed trades. The low win rate is acceptable because losses were tiny, but the huge giveback total says profit capture can improve.

Suggested modification: keep HTE behavior, but add tighter profit-protect variant for testing. Do not increase lots until OOS/replay confirms the lock does not cut the big winners too early.

### QG-O1 NIFTY Put Spread Theta Core

Keep paper-forward but do not scale. It made +Rs4793 over 12 closed trades, but average loss (-Rs2649) is much larger than average win (+Rs1416). That means one or two bad spreads can wipe several winners.

Suggested modification: preserve RES2 rich-vol/range gate, tighten green-to-red protection, and cap open concurrent QG-O1 exposure. Test a no-new-entry rule while an older same-strategy spread is open near expiry.

### IDX NIFTY VRP Call-Spread

Keep at smallest paper size only. It is barely positive: +Rs674 over 31 closed trades, profit factor 1.16. That is thin after friction and model error.

Suggested modification: require stronger IV/RV richness or better contract score before entry; reduce no-progress exits by testing shorter time windows versus stricter entry filtering.

### RAE SENSEX Trend Delta-1

Observe only. Four trades are not enough to call it profitable even though it is +Rs719.

Suggested modification: keep it paper-only, collect sample, and add a minimum realized move/cheap-IV gate before entry. Do not scale from n=4.

### Tail Hedge NIFTY Far-OTM Put Spread

Do not keep it as a hold-to-expiry profit lottery. It has -Rs2169 over 10 closed trades, and all 10 closed trades were green at some point; 6 closed red. That is exactly the profit-giveback disease.

Suggested modification: define its purpose. If it is a hedge, cap daily/weekly bleed and accept that it is insurance. If it is a profit strategy, remove HTE behavior and add early profit capture around 1x debit plus a 35%-45% peak trail.

### IDX SENSEX VRP Put-Spread

Keep paused. The sample is large enough to judge: 99 closed, -Rs6903, win rate 53.5%, profit factor 0.75. This is not a hidden winner; it is a high-activity negative-expectancy system.

Suggested modification: no live tuning. Run an offline sweep and only re-enable if a specific DTE/time/regime/IV slice is positive after costs.

### RAE SENSEX Range Seller

Keep paused. Win rate is 61.5%, but P&L is -Rs2025 and profit factor 0.79. The losses are about twice the wins.

Suggested modification: same as SENSEX Put-Spread: offline slice study first. If retained, it needs a lower stop multiple or much stricter entry edge.

### RAE NIFTY Range Seller

Pause or observe-only. It is -Rs1799 over 24 closed trades with poor win rate and many no-progress/squareoff exits.

Suggested modification: require rich IV/RV and avoid entries that cannot reach TP before squareoff. Current shape is not earning enough premium.

### RAE BANKNIFTY Trend Delta-1

Pause. It has only two trades, both losses, and BANKNIFTY monthly expiry creates poor short-horizon option behavior for this book.

Suggested modification: do not trade BANKNIFTY intraday option theta/delta until a separate monthly-expiry-specific study passes.

### IDX NIFTY Mean-Reversion Fade

Kill or archive pending fresh OOS. Two trades, -Rs8225, both green first, one catastrophic debit-spread stop.

Suggested modification: if the idea is kept, convert it to a tiny observe-only experiment with hard rupee max loss and fast profit lock. Do not let one debit spread risk several days of book profit.

### QG-O4 SENSEX Call Spread Range Pilot

No 30-day sample. Do not infer edge.

Suggested modification: diagnose why live status produced no trades: signal gates, expiry/DTE, IV/RV gate, or archived template mismatch.

### QG-O11 NIFTY Regime Seller Credit Scalp

Archived. Do not wake it just because it once had attractive backtest notes; later friction and cost-floor work warned that the small-credit edge can vanish.

Suggested modification: rerun with realistic friction and current spread-builder cost floor before paper-forward.

### RAE BANKNIFTY Range Seller

Keep archived. Prior system knowledge says BANKNIFTY monthly-only expiry makes intraday theta reachability poor.

Suggested modification: no intraday seller form unless redesigned for multi-day hold and OOS validated.

### RAE NIFTY Trend Delta-1

No 30-day trades. It may be correctly standing down.

Suggested modification: inspect skipped signals before changing; do not loosen gates blindly.

### IDX NIFTY Long-Gamma

No 30-day trades. That can be correct if HIGH_VOL_CHOP did not occur.

Suggested modification: keep as a conditional hedge/sleeve only; avoid forcing trades in normal range days.

## Implementation Order

1. Add a paper-only strategy governor that labels strategies as scale, observe, pause, or kill from measured stats.
2. Add per-structure profit-lock profiles: credit spread, debit spread, HTE credit, hedge debit.
3. Tighten default green protection for paper tests and expose the computed lock level in Execution UI.
4. Run an offline sweep for SENSEX sellers before any re-enable.
5. Add Hermes weekly strategy cards that cite numbers and propose only non-trading pending actions.

## Bottom Line

The best near-term path is not to make every strategy trade more. It is to shrink the book to the few sleeves showing positive expectancy, prevent open winners from round-tripping, and force every paused loser to earn re-entry through measured replay/OOS evidence.
