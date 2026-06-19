# Hermes Agent Integration Roadmap

## Purpose

[[Hermes Agent]] should become a QuantG operator and research assistant, not the trading brain.

QuantG remains the source of truth for execution, P&L, readiness, broker state, strategy state, and live trading gates. Hermes may observe, explain, audit, schedule, research, draft recommendations, and write approved notes or tasks.

## Safety Contract

Hermes starts read-only.

Forbidden by default:
- Place, cancel, modify, or exit trades.
- Enable live trading.
- Change broker credentials or broker settings.
- Change strategy, risk, or capital settings.
- Mutate production trading collections directly.

Allowed initially:
- Read approved QuantG API/tool outputs.
- Search the [[Knowledge Hub]].
- Generate reports and recommendations.
- Draft tasks, postmortems, and research notes.
- Notify the founder about readiness, data quality, and risk issues.

## Recommended Architecture

Hermes should run as a sidecar agent around QuantG:

`Hermes sidecar -> QuantG read-only tool gateway/API or MCP -> QuantG runtime/DB snapshots -> reports and recommendations`

Do not route live order execution through Hermes. Keep deterministic QuantG services responsible for trading.

## Staged Program

### Stage 0 - Design And Safety Contract

- TASK-H001: Write Hermes integration design doc.
- TASK-H002: Create Hermes safety policy.

Output:
- Allowed tools.
- Forbidden actions.
- Audit requirements.
- Rollout gates.

### Stage 1 - Hermes Sidecar

- TASK-H003: Install Hermes in an isolated environment.
- TASK-H004: Add Hermes deployment runbook.

Default recommendation:
- Same VPS sidecar first.
- In-app first, Telegram second.
- Strict read-only access to trading/runtime state.

### Stage 2 - QuantG Read-Only Tool Gateway

- TASK-H005: Design read-only tool schema.
- TASK-H006: Implement internal read-only agent API or MCP gateway.
- TASK-H007: Add agent tool audit logs.

Initial tools:
- `get_live_readiness`
- `get_feed_status`
- `get_token_status`
- `get_open_positions`
- `get_today_orders`
- `get_today_fills`
- `get_skipped_signals`
- `get_strategy_scorecard`
- `get_daily_report`
- `get_recent_alerts`
- `search_wiki`
- `get_backtest_summary`

Every tool response should include source, timestamp, user/account, stale-data flag, confidence, and warnings.

### Stage 3 - Daily Operator Reports

- TASK-H008: Market Open Readiness Report.
- TASK-H009: Intraday Health Watch.
- TASK-H010: EOD Trading Report.

The reports should identify token expiry, feed stalls, no-trade days, abnormal loss, stale positions, readiness changes, and strategy performance.

### Stage 4 - In-App Hermes Analyst

- TASK-H011: Add Hermes mode to Ask QuantG Agent.
- TASK-H012: Add source cards to agent answers.

The UI should show cited tool outputs, timestamps, stale-data warnings, and confidence rather than unsupported answers.

### Stage 5 - QuantG Hermes Skills

- TASK-H013: Create QuantG Hermes skill pack.
- TASK-H014: Sync selected wiki context with Hermes.

Initial skills:
- `quantg-live-readiness`
- `quantg-why-no-trade`
- `quantg-strategy-loss-review`
- `quantg-feed-token-diagnosis`
- `quantg-eod-report`
- `quantg-backtest-review`
- `quantg-vps-deploy-check`
- `quantg-incident-postmortem`

Rule:
- Wiki is context.
- DB/orders/fills/readiness are truth.

### Stage 6 - Strategy Research Assistant

- TASK-H015: Weekly Strategy Ranking Report.
- TASK-H016: Backtest Experiment Generator.
- TASK-H017: Strategy Experiment Ledger.

Track every strategy hypothesis, version, clean baseline, result, decision, and reason.

### Stage 7 - Approval-Gated Operations

- TASK-H018: Draft-only operations.
- TASK-H019: Founder approval queue.
- TASK-H020: Safe mutation framework.

Hermes may draft changes, but must not apply production trading changes without explicit approval.

Allowed later, after approval gates:
- Write wiki notes.
- Create TASKS.md entries.
- Create incident reports.
- Draft PR summaries.

Still forbidden:
- Live order placement.
- Live order cancellation.
- Live mode enablement.
- Broker credential changes.
- Core risk overrides.

### Stage 8 - Incident Commander

- TASK-H021: Automated incident timeline.
- TASK-H022: Postmortem generator.

Hermes should answer what happened, when it started, which strategies were affected, whether trading stayed safely gated, what evidence proves the diagnosis, and what should happen next.

## First Build Order

1. TASK-H001 Hermes integration design doc.
2. TASK-H005 read-only tool schema.
3. TASK-H006 internal read-only API.
4. TASK-H007 agent tool audit logs.
5. TASK-H003 isolated Hermes install.
6. TASK-H008 market open readiness report.
7. TASK-H010 EOD trading report.
8. TASK-H011 Hermes mode inside Ask QuantG Agent.

## Pending Founder Inputs

- Where should Hermes run: same VPS, separate VPS, or Windows first?
- First chat channel: in-app, Telegram, WhatsApp, Slack, or CLI?
- Model/provider: Nous Portal, OpenRouter, OpenAI, Anthropic, or local Ollama/vLLM?
- Should the Hermes program be added into TASKS.md as official tasks?
- Should Hermes remain permanently read-only or eventually allow approval-gated non-trading actions like writing wiki notes/tasks?

## Related Concepts

- [[Knowledge Hub]]
- [[Ask QuantG Agent]]
- [[Live Readiness]]
- [[Strategy Research]]
- [[Incident Postmortems]]
