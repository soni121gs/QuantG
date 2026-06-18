# QuantG Event Catalog Draft

Status: Stage 0 draft. Documentation only.

This catalog turns the CLAUDE.md section 11 architecture map into a concrete list of
candidate events. It does not define final event names or payload schemas. Those are
founder decisions before Stage 1.

## Ground Rules

- No runtime behavior changes are implied by this document.
- No fill, P&L, wallet, broker, or live-trading behavior changes are approved here.
- Stage 1 should convert one low-risk loop first inside the current Python process.
- Every future event must carry correlation and causation fields once the founder chooses the format.
- Current `backend/core/event_store.py` and the `core_events` collection are existing groundwork, not the final bus contract.
- Current `outbox_events` indexes exist in startup setup, but this draft does not assume an outbox implementation is active.

## Required Envelope Fields

Draft envelope fields for every future event:

| Field | Status | Notes |
|---|---|---|
| `event_id` | Proposed | Unique event id. Format TBD. |
| `event_type` | Proposed | Final naming convention TBD. |
| `schema_version` | Proposed | Start at `1`. |
| `occurred_at` | Proposed | UTC ISO timestamp. |
| `user_id` | Required where user scoped | Must be present for trading/user events. |
| `strategy_id` | Required where strategy scoped | Can be null for account/global events. |
| `correlation_id` | Founder decision | Ties one trading attempt from signal to fill/close. |
| `causation_id` | Founder decision | Points to the event that caused this event. |
| `idempotency_key` | Required where money moves | Existing order/fill keys should feed this. |
| `source_module` | Proposed | Module publishing the event. |
| `payload` | Founder decision | Shape and validation style TBD. |

## Strategy Lifecycle Events

| Draft event | Producer today | Consumers today | Current collection writes | Proposed owner | Dedupe key |
|---|---|---|---|---|---|
| `STRATEGY_EVALUATION_STARTED` | `strategy_runner` | Logs and strategy telemetry readers | None or `strategies.last_*` | Strategy Runner | `strategy_id + run_started_at` |
| `STRATEGY_EVALUATION_SKIPPED` | `strategy_runner` | Dashboard/strategy cards | `strategies.last_filter_reason`, counters | Strategy Runner | `strategy_id + reason + session_date` |
| `STRATEGY_SIGNAL_CANDIDATE_FOUND` | `strategy_runner` | Signal insert path | `strategies.last_signal_*` | Strategy Runner | `strategy_id + candle_time + action + target_symbol` |
| `STRATEGY_STATUS_UPDATED` | API routes/server startup tasks | Strategy runner, UI | `strategies.status`, config fields | Strategy Config API | `strategy_id + updated_at` |

Notes:

- Current runner directly mutates many `strategies` telemetry fields while also inserting `signals`.
- Stage 0 should not decide whether `strategies.today_pnl` remains on this collection; that belongs in collection ownership approval.

## Signal Lifecycle Events

| Draft event | Producer today | Consumers today | Current collection writes | Proposed owner | Dedupe key |
|---|---|---|---|---|---|
| `SIGNAL_QUEUED` | `strategy_runner` | `signal_manager` | `signals.insert_one(status=PENDING)` | Signal Manager after Stage 2, Strategy Runner today | `signal.id` |
| `SIGNAL_VALIDATION_FAILED` | `signal_manager` | UI, reports, diagnostics | `signals.status=FILTERED`, `rejection_reason`, `processed_at` | Signal Manager | `signal.id + reason_code` |
| `SIGNAL_PRIORITY_SKIPPED` | `signal_manager` conflict resolver | UI, diagnostics | `signals.status=SKIPPED_SIGNAL` | Signal Manager | `signal.id + selected_signal_id` |
| `SIGNAL_DISPATCH_STARTED` | `signal_manager` | Order path | None today | Signal Manager | `signal.id` |
| `SIGNAL_PROCESSED` | `signal_manager` | Strategy cards, execution state | `signals.status=PROCESSED`, `order_id`; `strategies.order_count_today` | Signal Manager | `signal.id + order_id` |
| `SIGNAL_EXECUTION_SKIPPED` | `signal_manager` | UI, diagnostics | `signals.status=SKIPPED_SIGNAL`, `rejection_detail` | Signal Manager | `signal.id + reason_code` |

Notes:

- Current statuses observed: `PENDING`, `FILTERED`, `SKIPPED_SIGNAL`, `PROCESSED`.
- Existing skip reasons include quality gates, strategy limits, group exposure, lower priority signals, duplicate idempotency, spread disabled, risk rejection, and preflight skips.

## Risk And Readiness Events

| Draft event | Producer today | Consumers today | Current collection writes | Proposed owner | Dedupe key |
|---|---|---|---|---|---|
| `RISK_EVALUATION_STARTED` | `core.risk_manager`, server order path | Order dispatch | `risk_events` in some paths | Risk Manager | `order_intent_id or signal_id` |
| `RISK_ORDER_REJECTED` | `core.risk_manager`, server order path | Signal manager, UI | Return status plus `risk_events` in some paths | Risk Manager | `idempotency_key + risk_status` |
| `RISK_RESERVATION_CREATED` | server order path | Order submission, reconciliation | `risk_reservations.insert_one` | Risk Manager | `order_id + user_id` |
| `RISK_RESERVATION_CLOSED` | server order path/reconcile | Readiness/reporting | `risk_reservations.update_many` | Risk Manager | `order_id + final_status` |
| `LIVE_PREFLIGHT_FAILED` | signal manager live preflight | Signal manager/UI | `signals.status=SKIPPED_SIGNAL` | Live Readiness Checker | `signal.id + check_name` |
| `KILL_SWITCH_CHANGED` | ops/server routes | Risk manager, readiness UI | `risk_state.update_one` | Ops/Risk State | `_id + updated_at` |

Notes:

- `backend/core/event_store.py` already logs examples like `RISK_BLOCKED`.
- Stage 1 should avoid adding a second writer to `risk_state`.

## Order Lifecycle Events

| Draft event | Producer today | Consumers today | Current collection writes | Proposed owner | Dedupe key |
|---|---|---|---|---|---|
| `ORDER_INTENT_CREATED` | signal manager or API/server path | Risk/order manager | In-memory today | Order Router | Existing/scoped idempotency key |
| `ORDER_IDEMPOTENCY_BLOCKED` | signal manager/server/order manager | Signal manager/UI | Sometimes `signals` or skipped response | Order Manager | `user_id + idempotency_key` |
| `ORDER_ACCEPTED_LOCAL` | execution router/server path | Broker/paper adapter | `orders.insert_one` | Order Manager | `order.id` |
| `ORDER_SUBMITTED_TO_BROKER` | server live submit path | Reconciler/readiness | `orders.status` fields | Broker Adapter | `broker_order_id` |
| `ORDER_FILLED_PAPER` | `core.execution_router` paper adapter | Portfolio ledger | `orders.status=FILLED/PARTIAL_FILL`; fill doc passed to ledger | Paper Adapter | `order.id + fill.id` |
| `ORDER_REJECTED_LOCAL` | risk/ledger/execution router | Signal manager/UI | `orders.status=REJECTED` or skipped response | Order Router | `order.id + reason_code` |
| `ORDER_STATUS_RECONCILED` | broker reconciliation/server paths | Readiness/UI | `orders.status`, broker status fields | Reconciler | `broker_order_id + broker_status` |

Notes:

- Entry idempotency currently uses minute-granular keys.
- Exit idempotency currently uses `exit:{pos_id}:{reason[:20]}`.
- Live and paper should share one execution port later; only adapters differ.

## Fill Lifecycle Events

| Draft event | Producer today | Consumers today | Current collection writes | Proposed owner | Dedupe key |
|---|---|---|---|---|---|
| `FILL_RECEIVED` | Paper adapter, live broker callback/reconcile | Portfolio ledger | Fill doc passed to ledger | Execution Adapter | `fill.id or broker fill id` |
| `FILL_DUPLICATE_SKIPPED` | `PortfolioLedger.process_fill` | Paper adapter/order path | `processed_fill_ids` duplicate skip | Portfolio Ledger | `fill_id` |
| `FILL_REJECTED_BY_LEDGER` | Portfolio ledger | Paper adapter/order path | No accepted `trade_fills`; order may be rejected/refunded | Portfolio Ledger | `fill_id + reason` |
| `FILL_ACCEPTED` | Portfolio ledger | Wallet/order/reporting | `processed_fill_ids`, `trade_fills`, `strategy_positions`, `positions` | Portfolio Ledger | `fill_id` |

Notes:

- Ledger idempotency currently inserts `processed_fill_ids` before applying a fill.
- The paper adapter waits for ledger acceptance before crediting SELL proceeds.
- This is the safest template for Stage 2 single-writer ownership.

## Position Lifecycle Events

| Draft event | Producer today | Consumers today | Current collection writes | Proposed owner | Dedupe key |
|---|---|---|---|---|---|
| `POSITION_OPENED` | Portfolio ledger, spread lifecycle | Monitor, guardian, UI | `strategy_positions.insert_one`, `positions.upsert` | Portfolio Ledger | `position_id` |
| `POSITION_ACTIVATED_FROM_BROKER` | Portfolio ledger | Monitor, guardian, UI | `strategy_positions.status=OPEN`, `positions.upsert` | Portfolio Ledger | `position_id + fill_id` |
| `POSITION_ADDED_TO` | Portfolio ledger | UI/P&L | `strategy_positions.open_quantity`, averages | Portfolio Ledger | `position_id + fill_id` |
| `POSITION_EXIT_REQUESTED` | Position monitor, guardian, server close path | Order path | `strategy_positions.status=EXITING` in server close path | Position Lifecycle Owner TBD | `position_id + reason` |
| `POSITION_EXIT_REVERTED` | `position_monitor` | Monitor/guardian retry | `strategy_positions.status=OPEN` for stale `EXITING` | Position Lifecycle Owner TBD | `position_id + stale_cutoff` |
| `POSITION_CLOSED` | Portfolio ledger, spread lifecycle | P&L/reporting/UI | `strategy_positions.status=CLOSED`, `trade_fills`, `trades`, `positions` cleanup | Portfolio Ledger | `position_id + closing_fill_id` |
| `POSITION_RISK_PARAMS_DEFAULTED` | `position_guardian`, `position_monitor` | Exit logic/UI | `strategy_positions.tp_sl_tsl_config`, `deadline_at` | Position Lifecycle Owner TBD | `position_id + protection_version` |
| `POSITION_MARK_TO_MARKET_UPDATED` | `position_monitor`, spread monitor | UI/execution snapshot | `strategy_positions.unrealized_pnl`, `last_tick_at` | Position Valuation Owner TBD | `position_id + tick_time` |

Notes:

- `strategy_positions` is the highest-risk collection because multiple loops write it.
- Stage 0 must not choose the final owner without founder approval.
- Strong candidate: portfolio ledger owns durable position state; monitor/guardian publish exit requests and valuation observations instead of mutating directly.

## Wallet Events

| Draft event | Producer today | Consumers today | Current collection writes | Proposed owner | Dedupe key |
|---|---|---|---|---|---|
| `PAPER_WALLET_INITIALIZED` | `PaperWallet` | Paper order path, UI | `paper_wallets.insert_one` | Paper Wallet | `user_id` |
| `PAPER_WALLET_DEBITED` | `PaperWallet.debit` | Paper adapter | `paper_wallets.$inc`, `updated_at` | Paper Wallet | `order_id or position leg key` |
| `PAPER_WALLET_CREDITED` | `PaperWallet.credit` | Paper adapter, spread lifecycle | `paper_wallet_credits.insert_one`, `paper_wallets.$inc` | Paper Wallet | `order_id` |
| `PAPER_WALLET_CREDIT_DUPLICATE_SKIPPED` | `PaperWallet.credit` | Diagnostics | `paper_wallet_credits` duplicate skip | Paper Wallet | `order_id` |
| `PAPER_WALLET_RESET` | Profile/ops reset | UI/trading state | `paper_wallets.replace_one` | Paper Wallet | `user_id + reset_id` |

Notes:

- `paper_wallets` is already single-owned by `PaperWallet`.
- `paper_wallet_credits.order_id` provides atomic credit idempotency.

## Broker And Feed Events

| Draft event | Producer today | Consumers today | Current collection writes | Proposed owner | Dedupe key |
|---|---|---|---|---|---|
| `BROKER_TOKEN_UPDATED` | auth/API routes | Upstox gateway/readiness | `broker_keys.update_one` | Broker Credentials API | `user_id + broker + updated_at` |
| `BROKER_FEED_SUBSCRIBED` | server startup/feed path | Quote service, monitor | In-memory feed state, logs | Feed Gateway | `user_id + instrument_key` |
| `BROKER_FEED_HEALTH_CHANGED` | startup health loop | Readiness UI/order blocks | `gateway_health.update_one` | Feed Gateway | `user_id + state + date` |
| `BROKER_POSITION_RECONCILED` | `position_reconciler`, server reconciliation loops | Readiness/UI | `strategy_positions`, `positions`, `risk_state` | Reconciler | `user_id + broker_position_key + run_id` |
| `BROKER_RECONCILIATION_MISMATCH` | Reconciler/readiness checks | Risk manager/live readiness | `risk_state` | Reconciler/Risk State TBD | `user_id + mismatch_key` |

Notes:

- Reconciliation currently writes several contested state slices.
- Stage 2 should decide whether reconciler mutates positions or emits repair requests to the position owner.

## P&L And Reporting Events

| Draft event | Producer today | Consumers today | Current collection writes | Proposed owner | Dedupe key |
|---|---|---|---|---|---|
| `REALIZED_PNL_RECORDED` | Portfolio ledger, spread lifecycle | Dashboard, leaderboard, reports | `trade_fills`, `trades`, sometimes `strategies.today_pnl` | P&L Engine from `trade_fills` | `position_id + closing_fill_id` |
| `UNREALIZED_PNL_UPDATED` | Monitor/spread valuation | UI/execution snapshot | `strategy_positions.unrealized_pnl` | Position Valuation Owner TBD | `position_id + tick_time` |
| `DAILY_REPORT_GENERATED` | `position_monitor` EOD aggregation | Calendar/report routes | `daily_reports.update_one` | Reporting | `user_id + date` |
| `STRATEGY_SCORECARD_UPDATED` | Portfolio ledger/reporting paths | Strategy UI, analytics | `strategies` metrics and scorecard collections | Reporting/P&L Owner TBD | `strategy_id + trade_id` |

Notes:

- Canonical realized P&L is converging on `trade_fills`.
- `strategies.today_pnl` remains a contested cache and should not be treated as source of truth.

## Lowest-Risk Stage 1 Candidates

Candidate A: publish-only signal events.

- Convert `SIGNAL_QUEUED`, `SIGNAL_VALIDATION_FAILED`, `SIGNAL_PRIORITY_SKIPPED`, and `SIGNAL_PROCESSED` to append events while keeping current writes.
- Lowest money risk because it does not alter order, fill, wallet, or position behavior.
- Useful because signals already have stable ids and clear statuses.

Candidate B: publish-only risk rejection events.

- Normalize existing `risk_events` and `core_events` usage around a draft envelope.
- Low money risk if publish-only.
- Useful for "why did not trade" diagnostics.

Candidate C: publish-only position observation events.

- Monitor/guardian emit observations but still write current fields.
- Higher risk because the same loops already touch `strategy_positions`, the bug-zone collection.

Recommendation: Candidate A first, unless the founder prefers risk diagnostics first.

## Founder Decisions Required Before Stage 1

1. Event naming convention: uppercase snake case, dotted domain names, or another style.
2. Payload schema convention: plain TypedDict, Pydantic models, JSON Schema, or markdown-only contracts first.
3. Correlation id format: generated per strategy evaluation, per signal, per order intent, or per full trade lifecycle.
4. Causation id format: event id only, source record id, or both.
5. First loop to convert: signal manager, risk manager, position monitor, or another slice.
6. Whether `core_events` becomes the event log or remains diagnostics-only.
7. Whether `outbox_events` is reserved for Stage 4 or used earlier as an in-process outbox.
8. Who owns contested state slices before Stage 2 begins:
   - `strategy_positions`
   - `strategies.today_pnl`
   - `positions`
   - SQLite `option_state_ledger`

## Stop Conditions

Stop and ask the founder before:

- Finalizing event names.
- Finalizing payload fields.
- Choosing correlation or causation id format.
- Moving any writer away from `strategy_positions` or `strategies.today_pnl`.
- Changing fill, P&L, wallet, broker, or live-trading behavior.
- Introducing Redis, a message broker, or a new event-store technology.
