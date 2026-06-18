# QuantG Collection Ownership Map

Status: Stage 0 draft. Documentation only.

This map documents current MongoDB and legacy state ownership so Stage 1 and
Stage 2 can avoid adding new writers to contested state. It does not approve any
runtime migration, schema change, fill/PnL/wallet math change, or deployment.

## Ownership Rules

- Single-writer per state slice is the target invariant.
- Many modules may read a collection; only one module should mutate a given
  state slice after Stage 2.
- Contested collections remain contested until the founder approves ownership.
- `trade_fills` and `paper_wallets` are the model for safe ownership.
- Runtime writers below exclude one-off reset, migration, and scratch scripts.

## Risk Levels

| Level | Meaning |
|---|---|
| Critical | Multiple writers touch money, position lifecycle, or PnL truth. |
| High | Multiple writers can affect readiness, visible state, or order lifecycle. |
| Medium | Shared state, but lower immediate money risk. |
| Low | Single owner or mostly append-only/audit state. |

## Core Collections

| Collection | Current purpose | Current runtime writers | Current readers | Risk | Proposed single owner | Allowed readers | Founder decision |
|---|---|---|---|---|---|---|---|
| `users` | User accounts, roles, settings, paper mode | Auth/API routes, profile/settings routes, ops reset paths | Auth, strategy runner, risk/readiness, UI routes | Medium | Auth/User Settings API | All user-scoped services that need settings | Confirm whether ops can keep writing paper mode/reset fields directly. |
| `strategies` | Strategy definitions, status, config, telemetry, `today_pnl` cache | Server/routes, strategy runner, signal manager, trade frequency, reconciler, spread lifecycle | Strategy runner, signal manager, UI, reports, risk sizing | Critical | Strategy Config API for definitions; PnL Engine for PnL-derived fields | Runner, signal manager, UI, reports, risk manager | Contested: split config/status from telemetry and `today_pnl`, or keep one owner. |
| `orders` | Local order records for paper and live, idempotency keys | Server order path, execution router, Upstox quality/reconciliation paths, spread lifecycle | UI, execution snapshot, readiness, reconciliation, reports | High | Order Manager / Order Router | Signal manager, broker adapter, reconciler, UI, reports | Confirm whether spread lifecycle may write leg audit orders or must publish requests to Order Manager. |
| `strategy_positions` | Durable strategy position lifecycle and option position state | Server close/recovery paths, strategy runner, position monitor, position guardian, position reconciler, spread lifecycle, portfolio ledger | Monitor, guardian, execution snapshot, dashboard, risk/readiness, reports | Critical | Position Ledger / Position Lifecycle Owner | Monitor, guardian, reconciler, UI, reports, risk manager | Contested: founder must choose final owner before Stage 2. |
| `positions` | Simplified UI mirror for paper/broker positions | Portfolio ledger/server, position reconciler | UI, risk dashboard, paper account views | High | Projection Builder / Execution Snapshot, or Position Ledger if mirror remains | UI routes, execution snapshot | Contested: keep as writable mirror or rebuild from `strategy_positions`/broker positions. |
| `paper_wallets` | Paper trading balance and aggregate debit/credit totals | `core.paper_broker.PaperWallet` | Paper adapter, profile, dashboard/readiness | Low | Paper Wallet | UI, paper adapter, reports | No current decision needed; keep as single-owned template. |
| `paper_wallet_credits` | Idempotency log for wallet credits | `core.paper_broker.PaperWallet.credit` | Paper wallet diagnostics | Low | Paper Wallet | Diagnostics/reporting | No current decision needed. |
| `signals` | Strategy signal queue and processed/filtered/skipped signal records | Strategy runner, signal manager, server/routes | Signal manager, UI, reports, readiness | High | Signal Manager after Stage 2; Strategy Runner only publishes signal candidates | Runner, UI, reports, execution diagnostics | Confirm whether Stage 1 starts with publish-only signal events. |
| `broker_keys` | Upstox OAuth/access tokens per user | Broker auth/API routes | Upstox gateway, readiness, live preflight | Medium | Broker Credentials API | Gateway, readiness checker, live order path | No major dispute, but token refresh ownership should be explicit. |
| `live_arm_state` | Per-user live trading armed/disarmed state | Live readiness/arm API routes | Risk manager, live preflight, readiness UI | Medium | Live Readiness API | Risk manager, order router, UI | Founder-gated; never auto-arm. |
| `risk_state` | Kill switch and reconciliation state | Server/ops routes, position reconciler | Risk manager, readiness UI, order path | High | Risk State Service | Risk manager, order router, readiness UI, reconciler | Confirm whether reconciler writes state or emits mismatch events. |
| `risk_reservations` | Pre-order capital/risk reservations | Server order path/pretrade risk | Order path, reconciliation, readiness | High | Risk Manager | Order router, reconciler, UI/readiness | Confirm if reservations move under Risk Manager before event bus. |
| `risk_reservation_locks` | Temporary lock for reservation updates | Server risk reservation path | Server risk reservation path | Medium | Risk Manager | None except diagnostics | Should disappear or shrink after single-writer ownership. |
| `risk_events` | Pretrade/risk audit events | Server order path, risk paths | Ops/risk dashboard, diagnostics | Low | Risk Manager | UI, AI diagnostics, reports | Decide relationship with `core_events`/future event store. |
| `trade_fills` | Canonical fill and realized PnL source | Portfolio ledger and spread lifecycle logical fill path | PnL, dashboard, leaderboard, reports, scorecards | Low today, critical importance | Portfolio Ledger / PnL Engine append path | All PnL/reporting readers | Treat as source-of-truth template; decide whether spread lifecycle must route through ledger for writes. |
| `processed_fill_ids` | Fill idempotency barrier | Portfolio ledger | Portfolio ledger only | Low | Portfolio Ledger | Diagnostics only | No current decision needed. |
| `trades` | Round-trip trade summary rows | Portfolio ledger, spread lifecycle | Reports, scorecards, analytics | Medium | Reporting/PnL Projection | UI, reports, analytics | Decide whether `trades` remains a projection from `trade_fills`. |
| `daily_reports` | EOD per-user trading summaries | Position monitor EOD job | Calendar/report routes | Low | Reporting Service | UI/report routes | No urgent dispute, but EOD should read canonical PnL only. |

## Market And Broker Collections

| Collection | Current purpose | Current runtime writers | Current readers | Risk | Proposed single owner | Allowed readers | Founder decision |
|---|---|---|---|---|---|---|---|
| `upstox_instruments` | Instrument master/cache | Instrument sync/import paths | Option selector, monitor fallback, resolver | Medium | Instrument Master Sync | Option selector, resolver, quote service | Confirm sync cadence and owner. |
| `paper_quote_cache` | Paper quote snapshot keyed by instrument | Quote/feed/paper quote paths | Position monitor, guardian, paper execution | Medium | Quote Service | Monitor, guardian, order router | Confirm whether cache writes belong only to Quote Service. |
| `gateway_health` | Upstox feed/gateway health | Server startup health loop | Readiness UI, live preflight | Medium | Feed Gateway | Readiness, order path, ops UI | No major dispute. |
| `broker_oauth_states` | OAuth CSRF/state tracking | Broker auth route | Broker auth route | Low | Broker Credentials API | Broker auth only | No current decision needed. |
| `upstox_exit_all_events` | Audit of broker exit-all action | Server broker route | Ops/audit | Low | Broker Adapter/Ops | Ops/audit | No current decision needed. |
| `historical_chains` | Stored historical option chain data | AutoResearch/backtest import paths | Backtest/research | Low | Research Data Service | Backtest, analytics | Outside Stage 0 money path. |
| `vix_history` | VIX/regime history | Daily scheduler/market data path | Market hub/regime analytics | Low | Market Data Service | Market hub, regime engine | Outside Stage 0 money path. |

## Diagnostics And Agent Collections

| Collection | Current purpose | Current runtime writers | Current readers | Risk | Proposed single owner | Allowed readers | Founder decision |
|---|---|---|---|---|---|---|---|
| `core_events` | Existing structured diagnostics/event log | `backend/core/event_store.py` callers | AI diagnostics, future audit | Low today | Event Store | All diagnostics/readers | Decide whether this becomes the Stage 4 event store or remains diagnostics-only. |
| `outbox_events` | Indexed future outbox/event staging collection | Unknown/limited current use | Future event bus | Low today | Event Bus/Outbox | Event dispatcher, diagnostics | Decide whether to use before Stage 4. |
| `skipped_signals` | Aggregated skipped signal diagnostics | Server/reset/diagnostic paths | UI diagnostics | Low | Signal Diagnostics | UI, AI diagnostics | Could become projection from signal events. |
| `option_selector_decisions` | Option selector decision audit | Strategy runner | Diagnostics/research | Low | Option Selector | UI/diagnostics/research | No current dispute. |
| `agent_audit_logs` | AI agent audit trail | AI routes | AI/admin diagnostics | Low | AI Agent Service | Admin/AI diagnostics | No current dispute. |
| `system_config` | Global app config defaults | Server startup/config routes | Server/routes | Medium | Config Service | All services | Confirm whether agents may modify directly. |

## Legacy And Parallel State

| State | Current purpose | Current runtime writers | Current readers | Risk | Proposed owner | Allowed readers | Founder decision |
|---|---|---|---|---|---|---|---|
| SQLite `option_state_ledger` | Legacy/parallel option ledger state | Legacy option ledger paths | Some recovery/report paths | Critical if active | Deprecate or wrap under Position Ledger | Read-only during migration | Contested: founder must choose deprecate vs delete vs temporary bridge. |
| Legacy fill engine | Older fill processing path, reportedly fenced | Server legacy paths if still reachable | Recovery/fallback only | Critical if re-enabled | Deprecate under Portfolio Ledger | None except audit | Contested: founder must approve delete vs deprecate. |
| `_mongo_position_monitor_loop` | Superseded inline monitor loop | Server if still wired | Position lifecycle | Critical if active | Retire in favor of extracted monitor/ledger ownership | None after migration | Contested: founder must approve delete vs deprecate. |

## Proposed Ownership Boundaries

### Strategy Config API

Owns:

- Strategy definitions
- Strategy status/draft/live/paused state
- User-editable visual config

Should not own:

- Realized PnL
- Position lifecycle
- Order execution state

### Strategy Runner

Owns:

- Strategy evaluation attempts
- Signal candidate production
- Strategy evaluation telemetry until that telemetry is moved to events/projections

Should not own:

- Orders
- Positions
- PnL truth

### Signal Manager

Owns:

- Signal queue processing
- Signal validation/filter/skip/processed state
- Conflict resolution decisions

Should not own:

- Position writes
- Wallet writes
- Broker writes

### Order Manager / Order Router

Owns:

- Local order records
- Order idempotency
- Dispatch to paper/live adapters through one execution port

Should not own:

- Fill acceptance
- Wallet credit after exits
- Position closing math

### Portfolio Ledger / Position Ledger

Owns:

- Accepted fills
- Durable position lifecycle
- Fill idempotency
- Canonical position open/close state

Should not own:

- Strategy evaluation
- Broker token state
- UI-only presentation state

### Paper Wallet

Owns:

- `paper_wallets`
- `paper_wallet_credits`
- Wallet debit/credit/reset idempotency

Should not own:

- PnL calculations beyond wallet balance summaries
- Position lifecycle decisions

### PnL Engine / Reporting

Owns:

- Derived realized PnL from `trade_fills`
- Daily/monthly reports
- Scorecards and leaderboard projections

Should not own:

- Fill acceptance
- Wallet mutation
- Position mutation

### Reconciler

Owns:

- Broker/local comparison observations
- Reconciliation result events

Should not own without founder approval:

- Direct `strategy_positions` repair writes
- Direct `positions` mirror writes
- Direct `risk_state` mismatch writes

## Contested Collections Requiring Founder Approval

### `strategy_positions`

Current problem:

- Multiple loops can mutate status, risk fields, PnL fields, and recovery state.
- This is the main phantom-position and duplicate-exit bug zone.

Decision needed:

- Does Portfolio Ledger become the only writer for durable position status?
- Do monitor/guardian/reconciler publish requested actions instead of writing directly?
- Where should mark-to-market fields live if `strategy_positions` becomes durable state only?

### `strategies.today_pnl`

Current problem:

- It is a cache on a config collection, and multiple modules increment or set it.
- Canonical realized PnL is supposed to derive from `trade_fills`.

Decision needed:

- Remove/deprecate as a writable cache, or keep as a projection owned by PnL Engine.
- Decide whether risk gates read canonical `trade_fills` or a PnL projection.

### `positions`

Current problem:

- It is a UI mirror and can drift from `strategy_positions` or broker state.

Decision needed:

- Keep as a projection with one projection owner.
- Or remove as a write target and derive execution snapshots at read time.

### SQLite `option_state_ledger`

Current problem:

- Parallel state source beside Mongo `strategy_positions`.

Decision needed:

- Deprecate, delete, or bridge temporarily.
- No agent should choose this without founder approval.

## Safe Templates

### `trade_fills`

Why it works:

- Append-style source of truth for fills and realized PnL.
- DB-level fill idempotency exists through `processed_fill_ids`.
- PnL readers can converge on it instead of using mutable caches.

Stage 2 implication:

- Spread lifecycle should either route fill writes through Portfolio Ledger or be formally accepted as part of the same logical owner.

### `paper_wallets`

Why it works:

- Wallet mutation is concentrated in `PaperWallet`.
- Credit idempotency is guarded by `paper_wallet_credits.order_id`.
- Paper adapter credits only after ledger acceptance.

Stage 2 implication:

- Keep this boundary. Other modules should request wallet actions, not mutate balances directly.

## Stage 1 Safety Recommendation

Start Stage 1 with publish-only signal events:

- `SIGNAL_QUEUED`
- `SIGNAL_VALIDATION_FAILED`
- `SIGNAL_PRIORITY_SKIPPED`
- `SIGNAL_PROCESSED`

Reason:

- Signals already have stable ids.
- This path improves diagnostics without touching fill, PnL, wallet, broker, or position math.
- It avoids the highest-risk collections while testing event envelope and correlation decisions.

## Stop Conditions

Stop and ask the founder before:

- Assigning final ownership for contested collections.
- Removing or rewriting any existing writer.
- Moving PnL gates from `strategies.today_pnl` to another source.
- Changing fill acceptance or wallet credit timing.
- Changing exit order creation or position status transitions.
- Deleting or disabling legacy/parallel paths.
- Adding Redis, a broker, or any new infrastructure.
