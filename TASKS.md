# TASKS.md — QuantG Agent Work Queue

**Read AGENTS.md before starting any task.**
**Pick the first open task `[ ]` that matches your model tier. Mark `[~]` when starting, `[x]` when done.**

Legend: `[ ]` open · `[~]` in progress · `[x]` done · ⛔ blocked (prerequisite not done)

---

## PRIORITY 1 — Bug Fixes (Ship These First)

---

### TASK-001 — Fix strategy limits enforcement
- **Status**: `[x]` commit 37c25dc · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o / Codex)
- **Session size**: ~1 hour
- **Prerequisite**: None

**Problem**:
`validate_strategy_limits()` in `backend/signal_manager.py` returns success after finding the strategy but does NOT actually enforce:
- Per-strategy `max_trades_per_day` limit
- Per-strategy `cooldown_minutes` between trades
- Per-strategy `daily_stop_loss` threshold
- Global daily trade count cap

**Files to touch**: `backend/signal_manager.py` only

**Exact steps**:
1. Open `backend/signal_manager.py`, find `validate_strategy_limits` (or the equivalent validation function).
2. Read the full function and the strategy document schema — find where `max_trades_per_day`, `cooldown_minutes` fields live.
3. Add real enforcement:
   - Query `orders` collection: count today's filled orders for this strategy. If >= `max_trades_per_day`, return BLOCKED with reason.
   - Check the timestamp of the last order for this strategy. If within `cooldown_minutes`, return BLOCKED with reason.
   - Check today's realized P&L for the strategy. If below `daily_stop_loss` threshold, return BLOCKED with reason.
4. Log each block with: `logger.info(f"[LIMITS] strategy={strategy_id} blocked: {reason}")`

**How to verify**:
```bash
# Start backend locally or check VPS logs after deploy
# Set a strategy max_trades_per_day=1, fire two signals — second must be blocked
# Look for: [LIMITS] strategy=... blocked: ...
docker-compose logs backend --tail=50 | grep LIMITS
```

**Commit format**:
```
fix: enforce strategy cooldown, max_trades_per_day, daily_stop_loss in signal validation

Task: TASK-001
Tier: 2
Files changed: backend/signal_manager.py
```

---

### TASK-002 — Block duplicate exit orders before order creation
- **Status**: `[x]` commit 6fd78cc · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o / Codex)
- **Session size**: ~45 min
- **Prerequisite**: None

**Problem**:
Currently duplicate exit orders are rejected by the ledger AFTER an order row is already created in the `orders` collection. In live trading this creates phantom order rows and risks duplicate fills. The idempotency check must happen BEFORE `order_manager.create_order()` is called.

**Files to touch**: `backend/core/execution_router.py` or wherever exit orders are dispatched (grep for `exit` + `create_order`).

**Exact steps**:
1. Grep: `grep -n "exit\|create_order" backend/core/execution_router.py`
2. Find the exit order dispatch path.
3. Before calling `create_order`, query the `orders` collection: check if an order with the same exit idempotency key (`"exit:{pos_id}:{reason[:20]}"`) already exists with status PENDING or FILLED.
4. If yes: log and return early — do NOT call `create_order`.
5. Add test in `backend/tests/test_audit_fixes.py` for this guard.

**How to verify**:
```bash
python -m pytest backend/tests/test_audit_fixes.py -v
# Also check: no duplicate rows in orders collection for same position exit
```

**Commit format**:
```
fix: check exit idempotency key before order creation, not after ledger rejection

Task: TASK-002
Tier: 2
Files changed: backend/core/execution_router.py, backend/tests/test_audit_fixes.py
```

---

### TASK-003 — Unify option quality gate into single entry check
- **Status**: `[x]` commit 6415dba · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o / Codex)
- **Session size**: ~1.5 hours
- **Prerequisite**: None

**Problem**:
Two separate systems are evaluating option quality — one marks contracts as selected, another marks them as `quality_readiness: BLOCK`. Paper trades are going through despite the BLOCK signal. There is no single authoritative gate that says: "this contract is tradeable right now."

**Files to touch**: `backend/core/option_selector_v2.py`, `backend/signal_manager.py` (entry gate call site)

**Exact steps**:
1. Read `backend/core/option_selector_v2.py` fully — find the quality scoring logic.
2. Find where `quality_readiness` is set and where it is currently checked (or NOT checked) before order placement.
3. Create or enforce a single function `is_contract_tradeable(symbol, ltp, quote_age_seconds, spread_pct, volume) -> (bool, str)` that returns (tradeable, reason).
4. Enforce this check in `signal_manager.py` at the point of entry — before order is dispatched. If not tradeable, log and return FILTERED with reason.
5. Thresholds (use existing values or these defaults): `quote_age < 30s`, `spread_pct < 2%`, `volume > 500 contracts`.

**How to verify**:
```bash
python -m pytest backend/tests/test_option_selector_v2.py -v
# After deploy: grep logs for FILTERED with quality reason
docker-compose logs backend --tail=100 | grep "quality\|FILTERED"
```

**Commit format**:
```
fix: enforce single option quality gate before order dispatch; filter stale/wide/low-volume contracts

Task: TASK-003
Tier: 2
Files changed: backend/core/option_selector_v2.py, backend/signal_manager.py
```

---

## PRIORITY 2 — Architecture Foundation (Enables All Future Agent Work)

---

### TASK-004 — Create backend/config.py with all tunable constants
- **Status**: `[x]` commit 6415dba · 2026-06-11
- **Tier**: 1 (Any model — Haiku, Codex, GPT-4o mini)
- **Session size**: ~1 hour
- **Prerequisite**: None

**Problem**:
Tunable constants (lot sizes, cooldown defaults, risk limits, quote age thresholds, spread limits) are scattered through `server.py`, `risk_manager.py`, `market_domains.py`, `signal_manager.py`. Tier 1 agents cannot change a config value without reading 15k lines.

**Files to touch**: Create `backend/config.py`. Grep for constants in existing files and reference them — do NOT remove them from their source files yet (that is a future refactor). This task is additive only.

**Exact steps**:
1. Create `backend/config.py`.
2. Grep for magic numbers and constants across backend:
   ```bash
   grep -n "= 65\|= 30\|cooldown\|max_trades\|daily_loss\|spread_pct\|quote_age\|500000\|lot_size" backend/server.py backend/core/*.py backend/signal_manager.py
   ```
3. Write a clean config module with sections: `MARKET`, `RISK`, `PAPER_TRADING`, `OPTION_QUALITY`, `STRATEGY_DEFAULTS`.
4. Each constant must have a one-line comment with its unit and meaning.
5. Import `os` at the top — any constant that could be an env var should fall back to env: `int(os.getenv("MAX_TRADES_PER_DAY", "5"))`.
6. Do NOT change any other file. This is documentation + future reference only for now.

**Template structure**:
```python
# backend/config.py
import os

class MARKET:
    NIFTY_LOT_SIZE = 65
    BANKNIFTY_LOT_SIZE = 30

class RISK:
    DEFAULT_DAILY_LOSS_LIMIT = int(os.getenv("DEFAULT_DAILY_LOSS_LIMIT", "5000"))  # INR
    MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "5"))
    DEFAULT_COOLDOWN_MINUTES = 15

class OPTION_QUALITY:
    MAX_QUOTE_AGE_SECONDS = 30
    MAX_SPREAD_PCT = 2.0
    MIN_VOLUME = 500

class PAPER_TRADING:
    STARTING_BALANCE = 500_000  # INR
    SLIPPAGE_PCT = 0.05         # 5 bps
```

**How to verify**:
```bash
python -c "from backend.config import MARKET, RISK; print(MARKET.NIFTY_LOT_SIZE, RISK.MAX_TRADES_PER_DAY)"
# Should print: 65 5
```

**Commit format**:
```
feat: add backend/config.py centralizing all tunable constants for agent-readable config

Task: TASK-004
Tier: 1
Files changed: backend/config.py
```

---

### TASK-005 — Write AGENT_ROUTER.md — symptom-to-file decision tree
- **Status**: `[x]` commit 6f61247 · 2026-06-11
- **Tier**: 1 (Any model)
- **Session size**: ~45 min
- **Prerequisite**: TASK-004 (so config.py file path is accurate)

**Problem**:
Agents waste time grepping the entire codebase to find where a bug lives. A routing document mapping symptoms → exact files and functions eliminates this cold-start cost for every future session.

**Files to touch**: Create `AGENT_ROUTER.md` at repo root.

**Exact steps**:
Build a decision-tree document with this structure for each symptom:

```markdown
## Symptom: "P&L is wrong / always 0"
- Primary file: backend/core/portfolio_ledger.py
- Secondary file: backend/server.py (search: "today_pnl")
- Collection: strategy_positions, trade_fills
- Common causes: fill not closing position, duplicate fill processed, wrong lot_size
- Test to run: python -m pytest tests/test_dashboard_truthfulness.py -v
- Tier: 2

## Symptom: "Order not placed / signal blocked"
- Primary file: backend/signal_manager.py → validate_strategy_limits()
- Secondary file: backend/core/risk_manager.py → _check_greeks_exposure()
- Look for: [LIMITS] or [RISK] in logs
- Test to run: python -m pytest tests/test_risk_controls.py -v
- Tier: 2
```

Build entries for ALL common symptoms. At minimum cover:
P&L wrong, order not placed, signal blocked, position stuck, paper fill not processing, strategy not activating, WebSocket disconnected, frontend not updating, option not selected, wrong strike selected, lot size wrong, wallet balance wrong, duplicate position, kill switch not triggering.

**How to verify**: Have another agent read the document and confirm they can find the right file for 3 different symptoms without opening any other file.

**Commit format**:
```
docs: add AGENT_ROUTER.md symptom-to-file decision tree for AI agent routing

Task: TASK-005
Tier: 1
Files changed: AGENT_ROUTER.md
```

---

### TASK-006 — Add 20 broker-free unit tests for core logic
- **Status**: `[x]` commit 6f61247 · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o / Codex)
- **Session size**: ~2 hours
- **Prerequisite**: None (tests must run without broker or live DB)

**Problem**:
The existing test suite (`backend/tests/`) has many integration tests that require a running server or broker. Agents cannot self-verify changes without tests that run locally in isolation.

**Files to touch**: `backend/tests/test_core_logic.py` (create new file)

**Exact steps**:
Write 20 tests in `backend/tests/test_core_logic.py` that cover pure functions only (no DB, no broker, no HTTP calls). Each test must pass with `pytest tests/test_core_logic.py -v` on a fresh clone.

Required test coverage:
1. `test_nifty_lot_size()` — market_domains returns 65 for NIFTY
2. `test_banknifty_lot_size()` — market_domains returns 30 for BANKNIFTY
3. `test_ce_symbol_check()` — `"CE" in "NIFTY 23200 CE 09 JUN 26"` is True
4. `test_ce_endswith_wrong()` — `"NIFTY 23200 CE 09 JUN 26".endswith("CE")` is False (document the pitfall)
5. `test_exit_idempotency_key_format()` — key = `f"exit:{pos_id}:{reason[:20]}"`, assert length <= 40
6. `test_entry_idempotency_key_format()` — sha256 key is 32 chars
7. `test_delta_proxy_ce_long()` — CE long qty=65 → delta = +0.5*65 = +32.5
8. `test_delta_proxy_pe_long()` — PE long qty=65 → delta = -0.5*65 = -32.5
9. `test_delta_proxy_ce_short()` — CE short qty=65 → delta = -32.5
10. `test_delta_proxy_pe_short()` — PE short qty=65 → delta = +32.5
11. `test_position_side_long()` — position_side="LONG" check pattern
12. `test_position_side_short()` — position_side="SHORT" check pattern
13. `test_pnl_long_position()` — long exit_price > entry_price → positive P&L
14. `test_pnl_short_position()` — short exit_price < entry_price → positive P&L
15. `test_paper_wallet_starting_balance()` — 500000 INR
16. `test_quote_age_threshold()` — age > 30s should flag as stale
17. `test_spread_pct_threshold()` — spread > 2% should flag as wide
18. `test_option_symbol_contains_space()` — NSE verbose format has spaces
19. `test_instrument_key_format()` — Upstox format: `"NSE_FO|<numeric_token>"`
20. `test_exit_qty_uses_open_quantity()` — exit_qty must come from open_quantity, not original qty

**How to verify**:
```bash
cd backend
python -m pytest tests/test_core_logic.py -v
# All 20 must pass. Zero imports from server.py.
```

**Commit format**:
```
test: add 20 broker-free unit tests for core trading logic

Task: TASK-006
Tier: 2
Files changed: backend/tests/test_core_logic.py
```

---

### TASK-007 — Create canonical P&L source function
- **Status**: `[x]` commit 6f61247 · 2026-06-11
- **Tier**: 3 (Opus / Claude — cross-module)
- **Session size**: ~3 hours
- **Prerequisite**: TASK-006 (tests must exist to verify this)

**Problem**:
P&L is computed in at least 4 places: `server.py` (dashboard endpoint), `portfolio_ledger.py` (fill processing), `strategy_runner.py` (today_pnl update), and the `positions` collection mirror. They disagree. The calendar system, capital allocator, and leaderboard all need one truth.

**Files to touch**: `backend/core/portfolio_ledger.py` (add canonical function), `backend/server.py` (update dashboard endpoint to call it)

**Exact steps**:
1. Read `backend/core/portfolio_ledger.py` fully.
2. Read all places in `server.py` that compute or return P&L (grep: `pnl\|today_pnl\|realized`).
3. Write a single async function in `portfolio_ledger.py`:
   ```python
   async def get_strategy_pnl_today(db, strategy_id: str, user_id: str) -> dict:
       """Returns: {realized_pnl, unrealized_pnl, total_pnl, trade_count, last_updated}"""
   ```
   Source of truth: `trade_fills` collection (not orders, not positions mirror).
4. Update the dashboard endpoint in `server.py` to call this function instead of computing inline.
5. Update `strategy_runner.py` `today_pnl` update to call this function.
6. Add tests in `backend/tests/test_core_logic.py` (or new file) that mock `trade_fills` data and assert the canonical function returns correct values.

**How to verify**:
```bash
python -m pytest tests/ -k "pnl" -v
# Deploy and check: dashboard P&L matches strategy cards P&L matches wallet change
```

**Commit format**:
```
feat: add canonical get_strategy_pnl_today() in portfolio_ledger; remove inline P&L duplication

Task: TASK-007
Tier: 3
Files changed: backend/core/portfolio_ledger.py, backend/server.py, backend/strategy_runner.py
```

---

## PRIORITY 3 — Server.py Route Extraction (Enables Tier 1/2 Work at Scale)

---

### TASK-008 — Extract strategy routes from server.py → routes/strategies.py
- **Status**: `[x]` commit 4d1bf01 · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~3 hours
- **Prerequisite**: TASK-006 (tests), TASK-007 (canonical P&L)

**Problem**:
All strategy CRUD and control endpoints live inside `server.py`. Any agent touching strategy logic must load 15k lines of context.

**Files to touch**: Create `backend/routes/strategies.py`. Edit `backend/server.py` (include new router, remove extracted routes).

**Exact steps**:
1. Grep: `grep -n "@api.get\|@api.post\|@api.put\|@api.delete\|@api.patch" backend/server.py | grep -i strat`
2. List every strategy-related endpoint. Typical candidates: GET /strategies, POST /strategies, PUT /strategies/{id}, POST /strategies/{id}/activate, POST /strategies/{id}/pause, DELETE /strategies/{id}.
3. Create `backend/routes/strategies.py` with an `APIRouter(prefix="/strategies")`.
4. Move each endpoint function into the new file. Bring all required imports.
5. In `server.py`, add: `from backend.routes.strategies import router as strategies_router` and `api.include_router(strategies_router)`. Remove the moved endpoints.
6. Verify all tests pass and the server starts clean.

**Critical**: Do NOT change any endpoint URL, request schema, or response schema. This is a pure move — zero behavior change.

**How to verify**:
```bash
python -m pytest tests/ -v
# Start backend locally, hit GET /strategies — same response as before
# grep server.py for old route decorators — they must be gone
```

**Commit format**:
```
refactor: extract strategy routes from server.py into routes/strategies.py

Task: TASK-008
Tier: 3
Files changed: backend/routes/strategies.py (new), backend/server.py
```

---

### TASK-009 — Extract signal routes from server.py → routes/signals.py
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-008 done

**Problem**: Same as TASK-008 but for signal-related endpoints.

**Files to touch**: Create `backend/routes/signals.py`. Edit `backend/server.py`.

**Exact steps**:
1. Grep: `grep -n "@api" backend/server.py | grep -i signal`
2. Move all signal endpoints to `backend/routes/signals.py` with `APIRouter(prefix="/signals")`.
3. Register in `server.py`, remove originals.
4. Run tests, verify server starts clean.

**How to verify**: Same pattern as TASK-008. Grep confirms endpoints gone from server.py.

**Commit format**:
```
refactor: extract signal routes from server.py into routes/signals.py

Task: TASK-009
Tier: 3
Files changed: backend/routes/signals.py (new), backend/server.py
```

---

### TASK-010 — Extract orders + positions routes → routes/orders.py + routes/positions.py
- **Status**: `[x]` commit 68033c8 · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~3 hours
- **Prerequisite**: TASK-009 done

**Files to touch**: Create `backend/routes/orders.py`, `backend/routes/positions.py`. Edit `backend/server.py`.

**Exact steps**: Same pattern. Grep for order-related and position-related endpoints. Move them. Register routers. Verify.

**Commit format**:
```
refactor: extract order and position routes from server.py into dedicated route files

Task: TASK-010
Tier: 3
Files changed: backend/routes/orders.py (new), backend/routes/positions.py (new), backend/server.py
```

---

### TASK-011 — Extract dashboard/P&L routes → routes/dashboard.py
- **Status**: `[x]` commit c6999de · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-007 (canonical P&L), TASK-010 done

**Files to touch**: Create `backend/routes/dashboard.py`. Edit `backend/server.py`.

**Commit format**:
```
refactor: extract dashboard and P&L routes into routes/dashboard.py

Task: TASK-011
Tier: 3
Files changed: backend/routes/dashboard.py (new), backend/server.py
```

---

## PRIORITY 4 — Calendar & Daily Report System

---

### TASK-012 — Backend: EOD aggregation job + daily_reports collection
- **Status**: `[x]`
- **Tier**: 2 (Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-007 (canonical P&L must exist)

**Problem**: There is no per-day summary of trading activity. The calendar UI (TASK-013) needs a backend data source.

**Files to touch**: Create `backend/routes/reports.py`. Edit `backend/position_monitor.py` (add EOD job).

**Exact steps**:
1. Create `backend/routes/reports.py` with these endpoints:
   - `GET /reports/daily/{date}` — returns one day's summary for the authenticated user
   - `GET /reports/daily` — returns last 30 days of summaries (for calendar month view)
2. MongoDB collection: `daily_reports`. Document schema:
   ```json
   {
     "user_id": "...",
     "date": "2026-06-11",
     "total_realized_pnl": 1250.0,
     "total_unrealized_pnl": -200.0,
     "trades_taken": 4,
     "signals_fired": 12,
     "signals_filtered": 8,
     "market_regime": "TRENDING",
     "best_strategy": {"name": "...", "pnl": 800.0},
     "worst_strategy": {"name": "...", "pnl": -200.0},
     "strategies": [...per-strategy breakdown...],
     "generated_at": "2026-06-11T15:35:00+05:30"
   }
   ```
3. In `backend/position_monitor.py`, add an `_run_eod_aggregation(db)` async function that:
   - Runs at 15:35 IST on market days
   - Calls `get_strategy_pnl_today()` for each active strategy
   - Writes/upserts a `daily_reports` document for today
   - Already in the monitor loop — add alongside the existing 30s check

**How to verify**:
```bash
# Hit GET /reports/daily/2026-06-11 — should return today's summary (or empty if no trades)
# Check MongoDB: db.daily_reports.find({date: "2026-06-11"}).pretty()
```

**Commit format**:
```
feat: add daily_reports collection, EOD aggregation job, and GET /reports/daily endpoints

Task: TASK-012
Tier: 2
Files changed: backend/routes/reports.py (new), backend/position_monitor.py
```

---

### TASK-013 — Frontend: Calendar page with daily P&L heatmap
- **Status**: `[x]`
- **Tier**: 2 (Sonnet / GPT-4o — frontend)
- **Session size**: ~3 hours
- **Prerequisite**: TASK-012 (backend reports API must exist)

**Problem**: No UI to see historical trading performance at a glance.

**Files to touch**: Create `frontend/src/pages/Calendar.jsx`. Edit `frontend/src/App.js` (route), edit sidebar nav component.

**Exact steps**:
1. Create `frontend/src/pages/Calendar.jsx`.
2. Month-view grid (7 columns, weeks as rows). Each day cell:
   - Green if realized P&L > 0, red if < 0, grey if no trades, white if future/weekend.
   - Show the P&L amount inside the cell if it fits.
   - Click on a day → slide-out panel on the right showing: trade count, per-strategy breakdown, best/worst strategy, market regime badge.
3. Navigation: prev/next month arrows. Default to current month.
4. Data: call `GET /reports/daily?month=2026-06` → array of day summaries.
5. Add route in `App.js`: `<Route path="/calendar" element={<Calendar />} />`.
6. Add "Calendar" link in the sidebar nav (wherever other nav items are).
7. Use existing Tailwind classes — do not introduce a new CSS framework.
8. No new npm packages — use only what is already installed.

**How to verify**:
```bash
# Rebuild frontend and open /calendar in browser
# Verify: month grid renders, days with trades are coloured, clicking a day shows detail
docker-compose build frontend && docker-compose up -d frontend
```

**Commit format**:
```
feat: add Calendar page with daily P&L heatmap and per-day trade drill-down

Task: TASK-013
Tier: 2
Files changed: frontend/src/pages/Calendar.jsx (new), frontend/src/App.js, frontend/src/components/Sidebar.jsx (or wherever nav lives)
```

---

## PRIORITY 5 — Capital Allocator & Regime Weighting

---

### TASK-014 — Adaptive capital allocator across strategies
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~4 hours
- **Prerequisite**: TASK-007 (canonical P&L), TASK-001 (strategy limits enforced)

**Problem**: All strategies receive equal capital regardless of recent performance. A strategy on a 3-trade win streak should get more capital than one that lost 3 times today.

**Files to touch**: Create `backend/core/capital_allocator.py`. Edit `backend/signal_manager.py` (use allocator at signal entry). Edit `backend/core/risk_manager.py` (read allocation before sizing).

**Exact steps**:
1. Create `backend/core/capital_allocator.py` with:
   ```python
   async def get_strategy_allocation_multiplier(db, strategy_id, user_id) -> float:
       """Returns a multiplier 0.5–2.0 based on recent performance.
       Hot (3+ wins today): 1.5x. Cold (2+ losses today): 0.5x. Neutral: 1.0x."""
   ```
2. Multiplier logic:
   - Fetch last 5 trades for the strategy today from `trade_fills`.
   - Win streak >= 3: return 1.5
   - Loss streak >= 2: return 0.5
   - Otherwise: return 1.0
   - Hard cap: never exceed 2.0x, never go below 0.25x.
3. In `risk_manager.py`, multiply the computed position size by `get_strategy_allocation_multiplier()` result before order dispatch.
4. Log: `logger.info(f"[ALLOC] strategy={strategy_id} multiplier={mult:.2f}")`

**How to verify**:
```bash
# Seed DB with wins/losses for a strategy, call the function, assert correct multiplier
python -m pytest tests/ -k "alloc" -v
docker-compose logs backend --tail=50 | grep ALLOC
```

**Commit format**:
```
feat: add adaptive capital allocator — hot strategies get 1.5x, cold get 0.5x

Task: TASK-014
Tier: 3
Files changed: backend/core/capital_allocator.py (new), backend/signal_manager.py, backend/core/risk_manager.py
```

---

### TASK-015 — Regime → capital weighting (not only blocking)
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 3 (Opus / Claude)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-014 (allocator must exist)

**Problem**: Market regime currently only blocks strategies (binary ALLOW/BLOCK). A trending day should give trending strategies MORE capital, not just allow them — and give range strategies LESS, not block them outright.

**Files to touch**: `backend/market_regime.py`, `backend/core/capital_allocator.py`

**Exact steps**:
1. Read `backend/market_regime.py` — find how regime is determined and returned.
2. Add a `get_regime_multiplier(strategy_type, current_regime) -> float` function:
   - TRENDING regime + trend-following strategy: 1.3x
   - TRENDING regime + range strategy: 0.5x
   - RANGE regime + range strategy: 1.3x
   - RANGE regime + trend strategy: 0.5x
   - VOLATILE regime + breakout strategy: 1.5x
   - Neutral/unknown: 1.0x
3. Multiply this into the final allocation in `capital_allocator.py`.
4. Keep the existing hard BLOCK for extreme regime mismatches (e.g., trying to go long in a strong downtrend) — do not remove that safety.

**Commit format**:
```
feat: convert regime gate from binary block to capital weight multiplier

Task: TASK-015
Tier: 3
Files changed: backend/market_regime.py, backend/core/capital_allocator.py
```

---

## PRIORITY 6 — Typed Module Contracts (Ongoing Quality)

---

### TASK-016 — Add TypedDict contracts for position and order data shapes
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-010 (route extraction) should be done first

**Problem**: Functions pass raw `dict` objects between modules. Agents reading `risk_manager.py` cannot know what fields are guaranteed without tracing callers.

**Files to touch**: `backend/core/models.py` (add TypedDicts). No other file changes in this task — just define the types.

**Exact steps**:
1. Open `backend/core/models.py` — check what already exists.
2. Add TypedDict definitions for the most-passed data shapes:
   - `PositionDoc` — all fields a position document can have
   - `OrderDoc` — all fields an order document can have
   - `FillDoc` — fields in a trade_fill record
   - `StrategyDoc` — fields in a strategy document
   - `SignalEvent` — what signal_manager passes downstream
3. Use `TypedDict` with `total=False` for optional fields.
4. Add a docstring to each TypedDict listing which collection it maps to.

**How to verify**:
```bash
python -c "from backend.core.models import PositionDoc, OrderDoc, FillDoc; print('OK')"
```

**Commit format**:
```
feat: add TypedDict contracts for position, order, fill, strategy, and signal shapes

Task: TASK-016
Tier: 2
Files changed: backend/core/models.py
```

---

### TASK-017 — Loss-streak throttling: wire strategy_loss_streaks into signal gate
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 2 (Sonnet / GPT-4o)
- **Session size**: ~1.5 hours
- **Prerequisite**: TASK-001 (strategy limits), TASK-014 (allocator)

**Problem**: `strategy_loss_streaks` collection exists but is not being read at signal entry time. Bad strategies keep firing.

**Files to touch**: `backend/signal_manager.py`

**Exact steps**:
1. In `validate_strategy_limits()` (after TASK-001 changes), add:
   - Query `strategy_loss_streaks` for this strategy's current streak.
   - If streak >= 3: set `allocation_multiplier = 0.25` and log `[THROTTLE]`.
   - If streak >= 5: return BLOCKED entirely (cooldown until next day).
2. Update `strategy_loss_streaks` on each confirmed losing trade close (in `portfolio_ledger.py` fill processing).

**Commit format**:
```
feat: wire loss-streak throttling into signal gate — 3 losses = 0.25x, 5 losses = blocked

Task: TASK-017
Tier: 2
Files changed: backend/signal_manager.py, backend/core/portfolio_ledger.py
```

---

## FUTURE / BACKLOG (Do Not Start Until Priority 1–4 Complete)

---

### TASK-018 — Typed contracts rollout across core/ modules
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 2
- **Prerequisite**: TASK-016

Replace `dict` parameter types with TypedDict references in `risk_manager.py`, `portfolio_ledger.py`, `execution_router.py`. Fold into future fixes — no dedicated session needed.

---

### TASK-019 — SENSEX regime detection
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 2
- **Prerequisite**: TASK-015

Extend `market_regime.py` to support SENSEX in addition to NIFTY/BANKNIFTY. Same regime logic, different index token.

---

### TASK-020 — Paper fill realism: slippage + partial fill model
- **Status**: `[ ]`
- **Tier**: 2
- **Prerequisite**: None (independent)

Improve `backend/core/paper_broker.py`: add configurable slippage (default 5 bps from config.py), add partial fill simulation for low-volume strikes, add quote timestamp validation before fill price is accepted.

---

## Completed Tasks

*(Move tasks here when done — include commit hash)*

| Task | Description | Commit | Date |
|---|---|---|---|
| TASK-001 | Fix strategy limits enforcement (cooldown + max_trades_day) | 37c25dc | 2026-06-11 |
| TASK-002 | Block duplicate exit orders before order creation | 6fd78cc | 2026-06-11 |
| TASK-003 | Enforce option quality gate in signal_manager dispatch | 6415dba | 2026-06-11 |
| TASK-004 | Add backend/config.py centralising all tunable constants | 6415dba | 2026-06-11 |
| TASK-005 | AGENT_ROUTER.md symptom-to-file decision tree | 6f61247 | 2026-06-11 |
| TASK-006 | 23 broker-free unit tests in test_core_logic.py | 6f61247 | 2026-06-11 |
| TASK-007 | Canonical get_strategy_pnl_today() in portfolio_ledger | 6f61247 | 2026-06-11 |

---

*Last updated: 2026-06-11*
*Total tasks: 20 (17 active + 3 backlog)*
*Open: 13 · In progress: 0 · Done: 7*
