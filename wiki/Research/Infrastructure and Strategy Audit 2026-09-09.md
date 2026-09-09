# Infrastructure and Strategy Audit 2026-09-09

Read-only production review; no trading configuration, orders, deployment or accounting mutations. Related: [[Strategy Profitability Review 2026-08-31]], [[Theta Reachability Law]], [[Cost Floor Law]].

Evidence: direct SSH, production container source, Mongo quantg, public health/version, local code and current persisted Hermes/Edge Lab reports on 2026-09-09. Financial amounts are INR and paper results. Windows below use UTC closed_at >= 2026-08-31, through the audit. Results are not a causal estimate of the August 31 changes; that day straddles the repair timestamp.

## Decision

The immediate priorities are capital/lifecycle correctness, bounded aggregate strategy risk, truthful diagnostics and deployment reconciliation. Buying more server capacity or adding ML is not supported as the next remedy by this evidence. Profit protection is operating, but cannot remove unbounded-by-budget loss exposure in a hold-to-expiry policy.

## 1. Risk budgets are not hard limits

HTE required_capital is 8,000. Three September 9 spreads had maximum losses 26,865.15, 26,620.75 and 29,302.00. The latter two overlapped from 04:50 to 06:59 UTC, totaling 55,922.75 of defined risk. Telemetry includes capital_cap_lots=0 and final_lots=1.

Production signal_manager.py:698-723 computes a zero affordable lot count then uses min(max(1, capital_cap), max(1, round(scaled))). core/spread_builder.py cap_lots_by_risk also deliberately floors at one lot. This is a confirmed design weakness: the labels imply stronger budget enforcement than exists. Require an affordable minimum lot or stand down, and enforce aggregate risk across overlapping underlying/direction/expiry exposure. Validate any changed strategy shape OOS and forward-paper before promotion.

## 2. QG-O1 expiry tail loss dominates recent results

75 closes since August 31 sum to -7,604.29, independently equal to 75 trade_fills realized_pnl. QG-O1 accounts for -22,468.16 over 7 closes. Two positions opened approximately 2m22s apart on September 3 and settled September 8:

| Position | Max loss before charges | Final P&L | Peak P&L |
|---|---:|---:|---:|
| pos_7964521fcf94 | 10,749.05 | -10,886.91 | 99.45 |
| pos_2d0b7d83377a | 10,173.15 | -10,353.76 | 41.60 |

Both were bullish NIFTY PE credit spreads with the same expiry and nearby strikes. Hold-to-expiry credit logic in position_monitor.py:1026 checks TP/profit protection then returns without a conventional stop-loss check. The recorded spread_sl_value is consequently not an operative stop for these positions. Tiny green peaks do not establish an executable net profit or show that tighter trailing would have prevented the losses. Research loss containment, entry validity and overlapping-risk limits first. Their combined modeled maximum risk was 20,922.20 against an 11,000 per-strategy allocation.

## 3. Cancelled positions retain margin

Wallet blocked_margin=80,799.55 exactly equals four BLOCKED paper_margin_blocks belonging to CANCELLED positions:

| Position | Amount |
|---|---:|
| pos_84dbc4e84d23 | 10,508.55 |
| pos_204a44276c70 | 11,196.90 |
| pos_dbb45b5c9913 | 30,162.60 |
| pos_5f48e3c15713 | 28,931.50 |

All four positions were updated to CANCELLED at 2026-08-28T05:06:03.620051+00:00, without corresponding margin release. The initiating action has not been identified. This can understate available capital. Audit their original cashflows and cancellation policy before a reconciled repair; do not simply adjust wallet balance.

The existing margin audit reports 78,089.05 drift because it subtracts 2,710.50 paid premium for the sole open debit spread from blocked_margin. Debit entry uses cash debits, not the credit-spread margin reservation path. Reconciliation must distinguish paid premium from reserved margin. This is not proof that the entire cash balance or lifetime P&L is correct.

## 4. Paper expiry settlement incorrectly receives execution slippage

Both QG-O1 settlements persist intrinsic short-minus-long value 200.00, but exit_value is 201.64 and 202.14. Production close_credit_spread applies adverse slippage whenever mode is paper, including reason=expiry-settlement. This introduces 245.70 of additional gross loss across the pair, before fees. The monitor correctly computes intrinsic legs, then the close path changes them as if market trades occurred. Separate cash settlement from execution modeling; also verify the authoritative final settlement price and settlement-specific fees. This defect does not explain away the roughly 21k economic loss.

NSE describes final exercise settlement at expiry: https://www.nseindia.com/static/products-services/equity-derivatives-settlement-mechanism

## 5. Hermes critical exit alarm is demonstrably false

September 9 exec.exit_reason_mix says zero price exits and all trades timed out. Its own evidence is debit-payback-tp=1 and profit-protect=3. The production _PRICE_EXIT_REASONS allowlist contains only spread-tp, spread-sl, trail-lock and dynamic-exit. The LLM amplified the faulty probe into a claim that risk management was non-functional.

September 9 fills, positions and daily report all agree on +4,374.43 from four closes. September 8 all three agree on -16,959.66 from twelve. Correct the deterministic taxonomy and historical narrative, and require narrative claims to agree with evidence. A day's missing finding is not proof the underlying issue was fixed: profit-giveback was marked resolved September 9 after the prior day's losses.

Geometry epochs also disagree: RAE NIFTY's top-level July 30 timestamp is used in a finding against 39 trades, while options.geometry_changed_at is August 31. Version reports against one authoritative configuration epoch, with post-change entry cohorts.

## 6. Deployment is behind completed local work

Public /api/version and VPS HEAD report 872defde. Local HEAD is 86ecd17 and includes the founder decision layer and execution quality ledger commits. Production /app/core/execution_quality.py is absent and db.execution_quality has zero rows. This is an undeployed feature, not evidence its recording code has failed. Reconcile reviewed commits and verify deployed API/UI plus actual telemetry on subsequent fills. No deploy was performed by this audit.

## 7. Strategy evidence remains mixed and insufficient for promotion

| Strategy | Closes since August 31 | Net P&L |
|---|---:|---:|
| QG-O1 | 7 | -22,468.16 |
| HTE NIFTY | 18 | +10,983.66 |
| Tail Hedge NIFTY | 5 | +10,820.65 |
| IDX NIFTY call spread | 23 | -1,612.69 |
| RAE NIFTY range seller | 8 | -1,348.07 |
| RAE SENSEX range seller | 2 | -1,198.77 |
| IDX SENSEX put spread | 11 | -2,632.80 |
| NIFTY reversal | 1 | -148.11 |

HTE and Tail Hedge deserve controlled further research, not scaling from these samples. Latest Edge Lab snapshot September 8 has no passing DSR among its 13 rows; QG-O1 OOS expectancy is -1,785.4 over 36 OOS returns despite positive aggregate historical P&L. The latest intraday OOS run (August 31, 2025 window) has six INSUFFICIENT_DATA verdicts. EOD held-position results cannot validate intraday exit rules. Historical research config, production side/selection and actual exit behavior need parity checks before drawing strategy conclusions. HTE's name says put spread, but September 9 production entries are bearish CE spreads.

## 8. Infrastructure and feed

Backend/frontend/Hermes/Mongo containers healthy; load about 0.27; disk 25% used with 72 GB free; public health responds OK. CORE_ENGINE_LIVE_ENABLED=false and LEGACY_EXECUTION_WRITES_ENABLED=false. The current 13 paused rows are schedule-paused with manual_paused=false, plus two archived rows; this is not a permanent loss-based shutdown.

September 9 feed_open_status records stale tick age 21,023 seconds at 09:20 IST. September 7 and 8 records were healthy. Later September 9 evaluations show live WebSocket candles and real trades. This supports an opening feed gap, not an all-day outage; exact missing minutes and recovered coverage remain unverified. Broker readiness should be established before session capture, with measured gap recovery.

## Repair acceptance order

1. Reconcile cancelled-position margin and cashflows, distinguish paid premium from reservations, and verify settlement math independently of trade-fill agreement.
2. Make per-trade and aggregate budgets enforceable; clearly expose when HTE ignores conventional stops. Replay loss-containment hypotheses without hindsight peak assumptions.
3. Repair Hermes exit taxonomy, overconfident narratives and configuration epochs.
4. Reconcile deployment and collect real execution-quality observations.
5. Run matched intraday OOS and controlled forward-paper for coherent strategy versions; prioritize demonstrated failure anatomy over adding parameters or ML.

Limitations: no complete UI render audit, disaster recovery/restore test, exhaustive accounting concurrency audit, new backtest, or full lifetime wallet reconciliation was performed. No profitability guarantee follows from these repairs.

## Repair implementation (2026-09-09)

The findings above are the pre-repair snapshot. TASK-AUDIT-0909 adds hard affordable/aggregate spread-risk gates and correlated-expiry protection; terminal-only margin release; intrinsic settlement without trading slippage and with exercise STT; corrected Hermes exit taxonomy, epoch selection and deterministic narratives; current-book intraday validation with an explicit exit-parity promotion blocker; and an idempotent, backed-up paper repair script. QG-O1 is designated for research standdown, not unvalidated exit tuning. Pending execution-quality and Founder UI commits are included in the intended release.

Local verification: 105 focused tests passed and the production frontend build passed. Deployment, historical repair and current-book validation results will be recorded below after execution. Profitability and fresh forward-paper evidence remain unproven.

### Additional cashflow root cause

The September 9 full reset-epoch check found another Rs6,657.38 of phantom wallet cash. It equals the five August 28 Ops cancellations exactly: four credit-spread entry receipts less one debit-spread entry payment. The old clear-stale endpoint marked filled positions CANCELLED and removed mirrors without reversing cashflows. The endpoint now refuses cleanup while paper positions are active and never cancels position records or deletes their mirrors/locks. The repair reverses only the five audited cashflows after checking they exactly explain the wallet residual against canonical fills plus the remaining open spread. It uses a backup, journal and atomic wallet marker.

Current-book intraday replay completed: 13 configurations, 3 negative diagnostic verdicts, 7 insufficient-data, and 3 unsupported multi-day exit-policy cases. Production exit parity is explicitly unverified and promotion_eligible=false for the run. The raw negative replay rows are NIFTY trend (44 trades, -Rs1,293.13), IDX NIFTY call spread (36, -Rs3,394.86), and NIFTY range seller (32, -Rs4,587.37). These are replay diagnostics, not evidence that a matched production strategy has been validated. No strategy was promoted or tuned to these outcomes.

## Verified deployment and repair outcome

- Backend release: `f5babf10` (`/opt/quantg-releases/f5babf1`); frontend built from `4ab7b1b` (unchanged in the follow-up backend fix). Public `/api/version` confirms the backend release. All five services healthy; live and legacy execution-write flags remain false.
- Original dirty `/opt/QuantG` checkout preserved. Inventory/code backup: `/opt/quantg-backups/audit0909-20260909T130644Z`. Active compose command uses `-p quantg -f /opt/QuantG/docker-compose.yml -f /opt/quantg-releases/audit0909-final.override.yml`. Use this clean-release override for this deployment; do not pull/reset the old dirty checkout or deploy it over the release. Original env/data/wiki mounts retained.
- Paper settlement correction: five closed records, net +Rs351.50, with matching canonical fills and updated reports. Intrinsic settlement no longer incurs paper trading slippage; long-holder exercise STT included. Remaining ordinary transaction charges are estimates.
- Cancelled-position margin released: Rs80,799.55 across four reservations. Separate cancelled-entry cashflow reversal: -Rs6,657.38 across five August 28 records. This is an accounting correction, not a new trading loss. Backups precede each apply, including `/data/audit-backups/paper-audit0909-20260909T133718Z.json`; durable repair journals live in `paper_state_audit`.
- Final wallet Rs457,546.21 = initial Rs500,000 - Rs39,734.33 realized across 689 canonical fill records - Rs2,719.46 remaining open debit-spread cashflow. Margin drift zero. Repeated apply produces zero corrections/releases. The existing open Tail Hedge `pos_a7aeed959eb4`, quantity 195, is preserved.
- Post-settlement recent-book result: 75 closes since August 31, -Rs7,319.17; QG-O1 seven closes, -Rs22,183.04. Earlier tables are the explicitly pre-repair snapshot. QG-O1 manually paused for expiry-tail research; no unvalidated exit/entry tuning or live promotion.
- 107 focused tests pass, frontend production build passes. Isolated real-Mongo smoke: one of two concurrent spread entries accepted; four leg-quality records with adverse slippage and fees; wallet/P&L identity passed. Test database removed. Production quality collection/indexes installed; actual post-release fill evidence remains zero after-hours and must accumulate naturally.
- Public login renders without browser errors. Authenticated Founder/Strategies rendering was blocked by lack of an authenticated browser session. Founder brief computed successfully against production data (September 9 +Rs4,374.43 across four closes). First startup had an external Gemini 503 in attribution distillation; deterministic diagnostics and trading gates remain independent of that service.
- Broad EOD rerun was stopped after about ten minutes; no completed new EOD verdict is claimed. Completed intraday replay is preserved in `intraday_options_oos_runs` and `/opt/quantg-releases/audit0909-intraday.log`. Matching multi-day exit policies, adequate data and fresh forward-paper evidence remain open under TASK-AUDIT-0909-R. Infrastructure correctness does not establish strategy profitability.
