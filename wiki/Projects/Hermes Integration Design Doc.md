---
topic: Hermes Agent Integration
tags: [hermes, agent, design, ai, safety]
date: 2026-06-20
---

# Hermes Integration Design Doc (TASK-H001)

Companion to [[Hermes Agent Integration Roadmap]]. This is the design + safety policy
(folds in TASK-H002). It is grounded in a live read of the existing agent code, not a
greenfield plan.

## 1. Thesis

Hermes is **not** a new system. QuantG already ships a read-only, in-app agent
("Ask QuantG Agent"). Hermes is the **rebrand + extension** of that agent into a
disciplined operator/analyst with a wider read-only tool surface and, later, an
approval-gated non-trading write path. QuantG stays the source of truth for execution,
P&L, readiness, broker state, and all trading gates. Hermes observes, explains, audits,
reports, and drafts — it never trades.

## 2. What already exists (verified)

| Capability | Where | Roadmap stage it satisfies |
|---|---|---|
| Read-only tool gateway (`agent_router`, `READ_ONLY_AGENT_TOOLS`, 8 tools) | `backend/routes/ai.py:35` | Stage 2 (partial) |
| Tool dispatcher with envelope (`status`, `started_at`, `finished_at`) | `backend/routes/ai.py:62` | Stage 2 |
| Deterministic local fallback summary (no-hallucination floor) | `backend/routes/ai.py:185` | Stage 4 |
| `PROPOSED_ACTION` parse + pending-action store (draft/approve seed) | `backend/routes/ai.py:232` | Stage 7 (embryo) |
| In-app analyst UI | `frontend/src/pages/AIBot.jsx` + `components/aibot/` | Stage 4 |
| Model: Gemini 2.5-flash, JSON mode, few-shot | `routes/ai.py` (`DEFAULT_GEMINI_MODEL`) | — |

Conclusion: Stages 2 and 4 are ~80% built. The work is mostly **plumbing + governance**,
not new architecture.

## 3. Tool surface — current vs target

Existing (8): `get_execution_snapshot`, `get_orders`, `get_positions`,
`get_active_strategies`, `get_upstox_status`, `get_market_data_status`,
`get_logs_errors`, `get_risk_snapshot`.

Target adds (most wrap endpoints that already exist):

| Target tool | Backing code | Work |
|---|---|---|
| `get_live_readiness` | `/ops/live-readiness` (`routes/ops.py:1091`) | wire |
| `get_strategy_scorecard` | `/ops/risk-scorecard` (`routes/ops.py:131`) | wire |
| `get_backtest_summary` | `/ops/options-backtest` (`routes/ops.py:147`) | wire |
| `get_today_fills` | `db.trade_fills` | query |
| `get_skipped_signals` | `db.signals` (FILTERED/SKIPPED) | query |
| `search_wiki` | Knowledge Hub Mongo collection | new query |
| `get_daily_report` | `daily_strategy_reporter` | wire |
| `get_recent_alerts` | (no source yet — define one) | new |
| `get_feed_status` / `get_token_status` | aliases of `get_market_data_status` / `get_upstox_status` | rename/alias |

## 4. Tool envelope contract (extend current envelope)

Every tool response MUST carry: `source`, `timestamp`, `user`/`account`, `stale` flag, `confidence`, `warnings`. The current envelope has source/timestamps/status; **add `stale`, `confidence`, `warnings`** to `_run_agent_tool` (`routes/ai.py:62`). Rule that drives the whole design: **Wiki is context; DB/orders/fills/readiness are truth.** Answers must cite tool output or say "unsure".

### Final JSON Response Schema Envelope
```json
{
  "name": "get_orders",
  "status": "ok",
  "source": "db.orders",
  "stale": false,
  "confidence": 1.0,
  "warnings": [],
  "user": "trader-id-123",
  "account": "trader-id-123",
  "timestamp": "2026-06-20T00:10:00.000Z",
  "started_at": "2026-06-20T00:09:59.900Z",
  "finished_at": "2026-06-20T00:10:00.000Z",
  "data": { ... }
}
```
If an exception occurs during tool execution, it returns:
```json
{
  "name": "get_orders",
  "status": "error",
  "source": "db.orders",
  "stale": true,
  "confidence": 0.0,
  "warnings": ["Execution failed: Connection timeout"],
  "user": "trader-id-123",
  "account": "trader-id-123",
  "timestamp": "2026-06-20T00:10:00.000Z",
  "started_at": "2026-06-20T00:09:59.900Z",
  "finished_at": "2026-06-20T00:10:00.000Z",
  "error": "Connection timeout"
}
```

## 5. Safety policy (TASK-H002, folded in)

Hermes is **read-only by default**. Forbidden at all stages:
- Place / cancel / modify / exit trades.
- Enable live trading (`CORE_ENGINE_LIVE_ENABLED`).
- Change broker credentials or broker settings.
- Change strategy / risk / capital settings.
- Mutate production trading collections directly.

Enforcement: Hermes only reaches the app through `READ_ONLY_AGENT_TOOLS`. No mutating
tool is ever registered in that list. The tool registry IS the security boundary — keep it
an allowlist, never a denylist.

Audit (TASK-H007): every tool call logs name, user, timestamp, args, status to an
`agent_tool_audit` collection. This is also the SEBI audit-trail seed.

## 6. Approval-gated writes — planned end state (Stage 7)

Founder decision (2026-06-20): Hermes will **eventually** allow approval-gated
**non-trading** writes — never live orders, cancels, live-mode, broker creds, or risk
overrides. Allowed after the approval queue ships:
- Write wiki notes.
- Create `TASKS.md` entries.
- Create incident reports / postmortems.
- Draft PR summaries.

Mechanism: extend the existing `PROPOSED_ACTION` path (`routes/ai.py:232`) into a founder
approval queue — Hermes emits a draft, it sits pending, nothing applies until the founder
approves in-app. Same draft-only pattern already present; it just gains a non-trading
write executor behind the approval gate.

## 7. Build order (this doc = step 1 done)

1. **TASK-H001 (this doc)** — design + safety policy. ✅
2. TASK-H005 — finalize read-only tool schema (envelope: + stale/confidence/warnings).
3. TASK-H006 — wire the missing Stage-2 tools (mostly existing ops endpoints).
4. TASK-H007 — `agent_tool_audit` logging.
5. TASK-H008 / H010 — market-open readiness + EOD reports.
6. TASK-H011 — "Hermes mode" label + source cards in AIBot UI.
7. (Later) Stage 7 — founder approval queue + non-trading write executor.

## 8. Still-pending founder inputs (do not assume)

- Sidecar runtime: same VPS / separate VPS / Windows-first. (In-app extension needs none.)
- Chat channel beyond in-app: Telegram / WhatsApp / Slack / CLI.
- Whether to add the Hermes program to `TASKS.md` as official tasks.

## Related

- [[Hermes Agent Integration Roadmap]]
- [[Knowledge Hub]]
- [[Ask QuantG Agent]]
- [[Live Readiness]]
