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
- **Frontend**: React + Tailwind. Pages in `frontend/src/pages/`.
- **Broker**: Upstox V3 only. WebSocket feed + REST orders.
- **Mode right now**: Paper trading. Live trading is disabled (`CORE_ENGINE_LIVE_ENABLED=false`).
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
| A frontend dashboard issue | `frontend/src/pages/Dashboard.jsx` |
| An ops / kill-switch issue | `backend/routes/ops.py` |
| Option selection / strike picking | `backend/core/option_selector_v2.py` |
| Config values (lot sizes, limits) | `backend/core/market_domains.py` |
| Position monitor loop | `backend/position_monitor.py` |
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

## 11. Active Program — Architecture Redesign (read CLAUDE.md §11)

The current major initiative is the **brain / event-bus redesign** mapped in **CLAUDE.md §11**.
That section is the single source of truth. Every agent (Claude, Codex, Antigravity, GPT, Gemini)
works this program the same way.

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

*Last updated: 2026-06-18*
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

