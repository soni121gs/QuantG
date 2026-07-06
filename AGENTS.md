# AGENTS.md — QuantG Multi-Agent Operating Guide

**Every AI agent (Claude, Codex, GPT-4o, Haiku, Gemini) must read this file before touching any code.**
**This file is the entry point. CLAUDE.md is the deep reference. TASKS.md is the work queue.**

---

## 1. Read These Three Files First — In Order

```
AGENTS.md   ← you are here — read fully
TASKS.md    ← find your task, claim it, execute it
CLAUDE.md   ← deep reference for architecture, file map, deployment
```

Do not open `server.py` or any other file until you have read all three.

---

## 2. What This App Is (30-Second Version)

QuantG is an NSE options algo-trading platform running on a VPS (82.180.145.183).

- **Backend**: FastAPI + Motor (async MongoDB). Main logic in `backend/server.py` (~15k lines) plus modules in `backend/core/`, `backend/routes/`, `backend/brokers/`.
- **Frontend**: React + Tailwind. Modular architecture: thin page orchestrators in `frontend/src/pages/`, presenter components in `frontend/src/components/{dashboard,strategies,ops,wiki,aibot}/`, centralized state via `frontend/src/contexts/ExecutionStateContext.jsx`.
- **Broker**: Upstox V3 only. WebSocket feed + REST orders. (Gotcha: REST `/market-quote/*` returns COLON-keyed data `NSE_INDEX:Nifty 50`; WS V3 uses PIPE keys `NSE_INDEX|Nifty 50`.)
- **Mode right now**: Paper trading. Live trading is disabled (`CORE_ENGINE_LIVE_ENABLED=false`).
- **Equity is LIVE on real data (paper)** as of 2026-06-22 — 10 NSE_EQ strategies trade alongside options (~24 live total). Old "equity is phantom / don't re-enable" cautions are obsolete; do NOT re-apply them. Equity strategies carry a trend re-entry patch in their `python_code`.
- **P&L is now REAL (2026-06-30)**: the recurring phantom-wallet over-credit class is fixed (equity exits are reduce-only) AND guarded — `PaperWallet.reconcile_if_flat` snaps the wallet to `initial + Σ realized_pnl` at EOD and logs CRITICAL on any drift. The truth source is `db.trade_fills`; **never compute P&L from the wallet balance alone**. A **directional-exposure cap** (`MAX_DIRECTIONAL_EXPOSURE_PER_UNDERLYING`) limits same-side concentration per underlying.
- **OOS BACKTESTING now exists + the current book has NO EDGE (2026-07-04)**: 2 years of real NSE option prices are ingested (`backend/scripts/bhavcopy_ingest.py`), and the OOS validator (`backend/core/eod_options_backtest.py`) graded every strategy — **0 of 11 have an out-of-sample edge**; a 72-config sweep found 0 winners. See CLAUDE.md §13.
- **🚫 GOVERNING RULE: do NOT tweak/tune the existing strategies.** That is the treadmill and it's proven futile. Any strategy change or new strategy MUST pass the OOS validator (`run_oos_validation.py` / `run_edge_sweep.py`) before deploy. Discipline: hypothesis → OOS backtest → forward-paper → live. Grade IDEAS on OOS expectancy, not daily paper P&L.
- **Live-book infra hardened (2026-07-06):** spread pyramiding stopped (anti-re-entry guard in `signal_manager.py`); spread P&L marks fixed (WS-subscribe `legs[]`, was 429→₹0.00); **manual exit + EXIT ALL now route through `_close_strategy_positions` — NEVER close a position via a generic opposite order under `manual_recovery` (ledger nets by strategy_id → phantom SHORT).** Tomorrow's paper-forward book is locked to **QG-O1 + QG-O5 only**. QG-O5 is now a tiny intraday bull-put credit-spread scalp; QG-O2/O3/O4/O6/O7/O8/O9/O10 and old book rows must remain archived and must not auto-wake from scheduler/startup restore. See CLAUDE.md §1 session-fixes + §6.
- **Database**: MongoDB at `mongodb://mongo:27017`, db name `quantg`.

For full architecture details → see `CLAUDE.md`.

---

## 3. Task Tier System — Match Your Model to the Task

Before picking a task, know your tier:

| Tier | Capability Required | Task Shape |
|------|---|---|
| **Tier 1** | Any model (Haiku, Codex, GPT-4o mini) | Single file, no logic change. Config values, docs, env vars, UI text. |
| **Tier 2** | Sonnet-class, GPT-4o, Codex | Single-module bug fix or feature. Max 2 files touched. Logic change confined to one domain. |
| **Tier 3** | Opus-class, Claude, senior review | Cross-module change, position lifecycle, new subsystem, broker integration. Requires full context. |

**If you are a lower-tier model and a task says Tier 3 — do not attempt it. Pick a Tier 1 or Tier 2 task instead.**

---

## 4. How to Claim and Complete a Task

### Step 1 — Pick an open task from TASKS.md
- Find a task with status `[ ]` (not started).
- Check that your model tier matches the task tier.
- Read the task's "Files to touch" and "How to verify" sections before writing any code.

### Step 2 — Read the relevant files first
- Use the File Map in CLAUDE.md to find exact files.
- Read the full function, not just the lines around the bug.
- Never edit a file you haven't read.

### Step 3 — Make the smallest possible change
- Edit only the files listed in the task. No scope creep.
- No new imports inside functions (unless circular import forces it).
- No comments explaining what the code does — only add a comment if the WHY is non-obvious.
- No new feature flags, no backwards-compatibility shims.

### Step 4 — Verify locally before committing
- Run the verification command listed in the task.
- For backend changes: confirm the server starts clean (no ERROR lines in startup logs).
- For frontend changes: confirm the page renders without console errors.
- If you cannot run the verification, say so explicitly — do not claim success.

### Step 5 — Commit with the standard format
```bash
git add <specific files only — never git add -A>
git commit -m "fix: <what you fixed and why in one line>

Task: TASK-<ID>
Tier: <1/2/3>
Files changed: <list>"
```

### Step 6 — Mark the task done in TASKS.md
Update the task's status checkbox from `[ ]` to `[x]` and add the commit hash.
Then push: `git push origin main`.

### Step 7 — Deploy (backend changes only need a backend rebuild)
```bash
ssh -i C:\Users\MG\.ssh\codex_quantg_vps root@82.180.145.183 \
  "cd /opt/QuantG && git pull origin main && docker-compose build backend && docker-compose up -d backend"
```
For frontend changes add: `docker-compose build frontend && docker-compose up -d frontend`

> **Known issue — mongo healthcheck flap:** `docker-compose up -d backend` can fail with `dependency failed to start: container quantg-mongo is unhealthy` even though mongo is fine (its mongosh-ping healthcheck times out at 5s). Workaround: `docker-compose up -d --no-deps backend` (verify backend was already connected first). Real fix is loosening the healthcheck in docker-compose.yml — do that OUTSIDE market hours; never `down -v`.

---

## 5. What You Must Never Do

- `git add -A` — always stage specific files. `.env` and secrets must never be committed.
- Edit `backend/core_legacy.py` — it is a rollback reference only.
- Enable `CORE_ENGINE_LIVE_ENABLED=true` — paper mode only.
- `docker-compose restart frontend` for UI changes — it does NOT rebuild JSX.
- `docker-compose down -v` — this wipes all MongoDB data.
- Add broad error handling or feature flags not required by the task.
- Write comments that explain what the code does (names do that). Only explain non-obvious WHY.
- Claim a task is done without running verification.
- Touch the position lifecycle or broker integration code unless the task explicitly requires it and you are Tier 3.

---

## 6. Critical Code Patterns — Memorise These

```python
# WRONG — options symbol check
symbol.endswith("CE")

# CORRECT
"CE" in symbol

# WRONG — position direction
pos.get("side")

# CORRECT
pos.get("position_side")   # values: "LONG" or "SHORT"

# WRONG — lot size
lot_size = 65  # hardcoded

# CORRECT
from backend.core.market_domains import resolve_domain_by_underlying
lot_size = resolve_domain_by_underlying(underlying).get_lot_size(underlying)

# WRONG — exit quantity
exit_qty = pos["qty"]

# CORRECT
exit_qty = pos.get("open_quantity")

# WRONG — average price
price = pos["average_price"]

# CORRECT
price = pos.get("average_price") or pos.get("average_buy_price") or 0

# WRONG — read an Upstox REST quote by the pipe key you sent
node = quotes["data"]["NSE_INDEX|Nifty 50"]      # REST returns COLON keys → None

# CORRECT — REST is colon-keyed; match colon/pipe/suffix
node = quotes["data"].get("NSE_INDEX|Nifty 50") or quotes["data"].get("NSE_INDEX:Nifty 50")

# WRONG — run single-leg LTP/staleness logic on a spread (it has option_type but no top-level instrument_key)
# CORRECT — skip spreads in position_guardian; position_monitor._process_spread_position owns them
if str(pos.get("structure")) in ("credit_spread", "debit_spread"):
    return

# Spread size is set by required_capital (lots_for_risk), NOT the 1-lot cap.
# The DAILY_CAP gate is trade_frequency._CLASS_CAPS (FREQ_CAP_*), NOT the max_trades_day field.
```

---

## 7. Quick File Lookup (Most Common)

| You want to fix... | Open this file |
|---|---|
| A strategy limit / cooldown bug | `backend/signal_manager.py` |
| A risk check rejection | `backend/core/risk_manager.py` |
| A position open/close bug | `backend/core/portfolio_ledger.py` |
| A paper fill / wallet bug | `backend/core/paper_broker.py` |
| An order routing bug | `backend/core/execution_router.py` |
| A P&L display bug | `backend/server.py` → search `pnl` |
| A frontend positions issue | `frontend/src/pages/Positions.jsx` |
| A frontend dashboard issue | `frontend/src/pages/Dashboard.jsx` + `components/dashboard/` |
| A frontend strategies issue | `frontend/src/pages/Strategies.jsx` + `components/strategies/` |
| A frontend ops/risk issue | `frontend/src/pages/OpsConsole.jsx` + `components/ops/` |
| A frontend wiki issue | `frontend/src/pages/Wiki.jsx` + `components/wiki/` |
| A frontend AI bot issue | `frontend/src/pages/AIBot.jsx` + `components/aibot/` |
| A frontend global state issue | `frontend/src/contexts/ExecutionStateContext.jsx` |
| A frontend data polling issue | `frontend/src/hooks/useExecutionState.js` |
| A frontend layout / sidebar | `frontend/src/components/Layout.jsx` |
| A frontend CSS / theme issue | `frontend/src/index.css` |
| An ops / kill-switch issue | `backend/routes/ops.py` |
| Option selection / strike picking | `backend/core/option_selector_v2.py` |
| Config values (lot sizes, limits) | `backend/core/market_domains.py` |
| Position monitor loop | `backend/position_monitor.py` |
| AI / Hermes backend API routes | `backend/routes/ai.py` |
| Hermes sidecar engine daemon | `hermes/agent.py` |
| Hermes deployment runbook | `docs/DEPLOY_HERMES.md` |
| Existing tests | `backend/tests/` |

---

## 8. Running Tests

```bash
# From the backend/ directory (activate venv first on Windows)
cd backend
.venv\Scripts\activate          # Windows
# or: source .venv/bin/activate  # Linux/Mac

# Run a specific test file (fastest)
python -m pytest tests/test_risk_controls.py -v

# Run all tests that don't need a live broker
python -m pytest tests/ -v --ignore=tests/test_live_readiness.py --ignore=tests/test_execution_bridge_upstox_only.py

# Run a single test function
python -m pytest tests/test_risk_controls.py::test_function_name -v
```

Tests that require MongoDB will be skipped automatically if no DB is running.
Tests in `backend/tests/` that are pure-logic (no DB, no broker) can always be run locally.

---

## 9. Commit Message Convention

```
fix: short description of what was broken and what fixed it       ← bug fix
feat: short description of new capability added                   ← new feature
refactor: short description of structural change (no behaviour)   ← restructuring
test: add/update tests for X                                      ← test-only
docs: update AGENTS.md / TASKS.md / CLAUDE.md                     ← docs only
```

Always include `Task: TASK-<ID>` in the commit body.

---

## 10. How TASKS.md Works

- Each task has a unique ID: `TASK-001`, `TASK-002`, etc.
- Status: `[ ]` = open, `[~]` = in progress (add your model name), `[x]` = done
- Every task has: Tier, estimated session size, files to touch, exact steps, verification command.
- Tasks are ordered by priority. Start from the top.
- If you finish a task and have time left, immediately pick the next open task.
- **Do not start a task if the prerequisite tasks listed are not yet done.**

---

## 11. Active Programs — what to work on now

**The source of truth for "what to pick up" is the `▶ OPEN TASKS INDEX` near the top of `TASKS.md`.** Read it first. As of 2026-06-30 there are two live initiatives plus backlog:

1. **🧠 Hermes Self-Improvement Loop (headline) — `HSI-11..54`** at the bottom of TASKS.md. The path to a self-improving trading brain: attribute every trade → grounded EOD analysis → scored/decaying lessons → OOS validation → human-gated advice. **START with `HSI-11` (Trade Attribution Engine)** — pure code, zero trading risk, unblocks everything. Two laws: *every claim backed by a computed number + sample size*; *no lesson influences trading until it passes an out-of-sample backtest* (judge-first). Hermes NEVER trades or edits code — read-only tools + approval-gated `pending_actions` only.
2. **📈 Win-Rate / Expectancy — `WR-3x..WR-7x`** (PRIORITY 0). Optimize **expectancy + Sharpe**, not win rate; weight the book toward measured edge.

### Backlog program — Architecture Redesign (read CLAUDE.md §11)

The **brain / event-bus redesign** mapped in **CLAUDE.md §11** is a parallel backlog initiative — do NOT start it unless the founder directs. That section is its single source of truth. Every agent (Claude, Codex, Antigravity, GPT, Gemini) works it the same way.

**Execution rules**
- Work the §11.10 migration ladder **one rung per PR** (0→6). Do not batch rungs.
- **Money-correctness rungs (Stages 0–2) ship before concurrency rungs (3–6).** Never reorder.
- Each stage is first written into TASKS.md as `TASK-###` entries (Tier, Files, Steps, Verify) **before** any code is written, and the breakdown is approved by the founder.
- Honor the five invariants in §11.8 — especially **single-writer per state slice**. Never add a second writer to a collection (see the §11.4 writer heat map).

**Stop-and-ask triggers (do NOT assume — ask the founder first)**
- Event names, payload schemas, or correlation/causation id format.
- Which module becomes the single owner of a contested collection (esp. `strategy_positions`, `strategies.today_pnl`).
- Deleting vs. deprecating any legacy path (legacy fill engine, `_mongo_position_monitor_loop`, SQLite `option_state_ledger`).
- ANY change to fill / P&L / wallet math.
- Introducing new infra (Redis, event-store technology).
- Before every VPS deploy of a stage.

**Deploy gate**
- Deploy per §4 of this file **only after the founder approves that stage's PR.** Tests pass first; tail logs after; report results.
- `CORE_ENGINE_LIVE_ENABLED` stays `false` — Stage 5 (live) is founder-gated, never agent-initiated.

---

*Last updated: 2026-06-30*
*Maintained by: platform owner. Update this file when new patterns emerge.*

---

## 12. Wiki & Auto-Memory Rules

- A central Knowledge Hub is stored under the `wiki/` directory in the repository root.
- **Topics**: Subdirectories (`YouTube transcripts/`, `Meeting transcripts/`, `Decisions/`, `Projects/`, `Trading Rules/`) organize context files.
- **Wikilinks**: Always use double-bracket links `[[Page Title]]` to cross-link concepts.
- **Auto-Memory Ledger**: The file `wiki/memory.md` is the system auto-memory record.
- **Agent Rule**:
  1. **At Startup**: Read `wiki/memory.md` and check recent summaries to understand context.
  2. **At Session Close**: You MUST append a row to the "Session Logs" table in `wiki/memory.md` with:
     - Date, model name.
     - Summary of changes and decisions made.
     - Challenges faced and how you fixed them.
  Never delete or overwrite past rows in `wiki/memory.md`; always append.
