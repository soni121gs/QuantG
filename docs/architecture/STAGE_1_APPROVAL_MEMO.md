# Stage 1 Approval Memo

Status: Founder approval required before implementation.

This memo closes Stage 0 and defines the safest first Stage 1 slice. It is not an
implementation plan for changing trading behavior. Stage 1 should be publish-only
first: add event records around an existing path while leaving current writes and
runtime decisions unchanged.

## Stage 0 Summary

Stage 0 produced two draft references:

- `docs/architecture/EVENT_CATALOG.md`
- `docs/architecture/COLLECTION_OWNERSHIP.md`

The event catalog names candidate event families across strategy evaluation,
signals, risk, orders, fills, positions, wallet, broker/feed, and PnL/reporting.
The ownership map identifies the highest-risk multi-writer state slices and the
proposed ownership boundaries needed before Stage 2.

## Main Finding

QuantG's biggest remaining app risk is not missing UI or missing strategy
features. It is state ownership.

The dangerous zones are:

- `strategy_positions`: many writers can touch position lifecycle, risk fields,
  mark-to-market fields, and recovery state.
- `strategies.today_pnl`: a mutable PnL cache on the strategy config collection.
- `positions`: a UI mirror that can drift from the strategy ledger or broker.
- SQLite `option_state_ledger`: parallel legacy state beside Mongo.

The safe templates are:

- `trade_fills`: append-style fill/PnL truth with fill idempotency.
- `paper_wallets`: single-owned wallet mutation through `PaperWallet`.

## Recommended Stage 1 Slice

Start with publish-only signal events.

Events:

- `SIGNAL_QUEUED`
- `SIGNAL_VALIDATION_FAILED`
- `SIGNAL_PRIORITY_SKIPPED`
- `SIGNAL_PROCESSED`
- `SIGNAL_EXECUTION_SKIPPED`

Why this first:

- Signals already have stable ids.
- Signal status transitions are visible and easy to verify.
- It improves the "why is it not trading?" audit path.
- It does not change order placement.
- It does not change fill acceptance.
- It does not change wallet credit/debit timing.
- It does not change PnL math.
- It does not touch broker/live execution.
- It avoids the highest-risk position writer migration until Stage 2.

## Stage 1 Non-Goals

Do not do these in Stage 1:

- Do not move writers between modules.
- Do not change `strategy_positions` ownership.
- Do not change `strategies.today_pnl` reads or writes.
- Do not change fill, PnL, wallet, position, or broker behavior.
- Do not introduce Redis or any external message broker.
- Do not enable live trading.
- Do not deploy without founder approval.

## Proposed Event Envelope

Recommended default:

```json
{
  "event_id": "evt_<uuid>",
  "event_type": "SIGNAL_QUEUED",
  "schema_version": 1,
  "occurred_at": "2026-06-18T00:00:00Z",
  "user_id": "...",
  "strategy_id": "...",
  "correlation_id": "corr_<signal_id>",
  "causation_id": "source:<source_record_id>",
  "idempotency_key": "...",
  "source_module": "signal_manager",
  "payload": {}
}
```

This is only a recommendation. Final schema is founder-approved.

## Recommended Decisions

| Decision | Recommendation | Reason |
|---|---|---|
| Event naming | UPPER_SNAKE_CASE | Matches existing status/reason style and current `CoreEventStore` uppercases event type. |
| Payload schema | Pydantic model per event family | FastAPI already uses Pydantic; gives validation without new infra. |
| Correlation id | `corr:<signal_id>` for Stage 1 | Signal events are the first slice; easy to trace. |
| Causation id | Previous event id when present, else `record:<collection>:<id>` | Works before a full event chain exists. |
| Event storage | Use existing Mongo `core_events` for Stage 1 publish-only | No Redis, no new broker, no infra churn. |
| First loop | Signal manager path | Low money risk and high diagnostic value. |
| Deploy | No deploy until approved after tests | Matches AGENTS.md deploy gate. |

## Founder Decisions Required

Please approve or override these before Stage 1 code starts:

1. Event naming convention:
   - Recommended: `UPPER_SNAKE_CASE`

2. Payload schema convention:
   - Recommended: Pydantic models in a small event-contract module

3. Correlation id format:
   - Recommended for Stage 1: `corr:<signal_id>`

4. Causation id format:
   - Recommended: previous event id when available, otherwise source record id

5. First loop to convert:
   - Recommended: signal manager publish-only events

6. Event persistence:
   - Recommended: existing Mongo `core_events` for Stage 1

7. `strategy_positions` ownership for future Stage 2:
   - Recommended direction: Portfolio Ledger owns durable position status
   - Monitor/guardian/reconciler publish requests or observations instead of
     writing durable lifecycle state directly

8. `strategies.today_pnl` ownership for future Stage 2:
   - Recommended direction: PnL Engine owns projections from `trade_fills`
   - Treat `strategies.today_pnl` as cache/projection, not source of truth

9. `positions` UI mirror:
   - Recommended direction: keep only as a projection, or rebuild snapshots from
     `strategy_positions` plus broker state

10. Legacy paths:
   - Recommended direction: deprecate before delete
   - Applies to legacy fill path, `_mongo_position_monitor_loop`, and SQLite
     `option_state_ledger`

## How Stage 1 Will Affect The App

If implemented as recommended, Stage 1 should affect the app this way:

- Trading behavior: no change.
- Paper orders: no change.
- Live orders: no change and still disabled unless founder-gated later.
- Wallet balance: no change.
- Position lifecycle: no change.
- PnL: no change.
- UI: no required visual change.
- Debugging: better, because signal transitions get structured audit events.
- Future AI agent diagnosis: better, because "why did not trade" can be traced
  through event rows instead of scattered logs and mutable fields.

## Verification Required For Stage 1

Minimum verification before any Stage 1 commit:

```bash
cd backend
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_audit_fixes.py -v
```

Additional verification after implementation:

- Trigger or seed one pending signal.
- Confirm the signal status remains the same as before.
- Confirm `core_events` contains the expected publish-only event rows.
- Confirm no changes to orders, fills, wallet, positions, or PnL.

## Approval Checklist

Stage 1 may begin only after the founder approves:

- [ ] Event naming convention
- [ ] Payload schema convention
- [ ] Correlation id format
- [ ] Causation id format
- [ ] First loop to convert
- [ ] Event persistence target
- [ ] Future owner direction for `strategy_positions`
- [ ] Future owner direction for `strategies.today_pnl`
- [ ] Future owner direction for `positions`
- [ ] Deprecate vs delete stance for legacy paths

## Current Recommendation In One Sentence

Approve a publish-only Stage 1 using existing Mongo `core_events`, Pydantic
event contracts, `UPPER_SNAKE_CASE` event names, and the signal manager as the
first slice; defer all writer migrations to Stage 2.
