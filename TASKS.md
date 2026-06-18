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
- **Status**: `[x]` commit this session · 2026-06-11
- **Tier**: 2
- **Prerequisite**: None (independent)

Improve `backend/core/paper_broker.py`: add configurable slippage (default 5 bps from config.py), add partial fill simulation for low-volume strikes, add quote timestamp validation before fill price is accepted.

---

## PRIORITY — Phase 2 UI Polish (surface the options-alpha features)

Context: Phase 2 (theta-aware exits, delta strikes, IV-rank gate, order-flow gate, credit spreads) is deployed; config + strategy/positions UI are done. These tasks make the engine's behaviour legible in the UI. All are frontend-leaning, low engine risk, gated/additive. Do in order; deploy + verify each. Frontend changes require `docker-compose build frontend && docker-compose up -d frontend` (a restart does NOT pick up JSX).

### TASK-023 — Friendly labels for Phase 2 exit/skip reasons
- **Status**: `[x]` commit 86705ba · 2026-06-16
- **Tier**: 2
- **Session size**: ~45 min
- **Prerequisite**: None

**Problem**: New reasons render as raw codes (`theta-decay-10m`, `theta-no-progress-8m`, `spread-tp`, `spread-sl`, `IV_RANK_GATE`, `IV_RANK_SHADOW`, `ORDERFLOW_GATE`, `ORDERFLOW_SHADOW`, `CREDIT_SPREADS_DISABLED`). `Strategies.jsx` already has a `noticeFor()` mapper for older reasons; the Phase 2 ones aren't covered, and Orders/Positions show reasons raw.

**Files to touch**: `frontend/src/lib/` (new shared `reasonLabels.js` helper), `frontend/src/pages/Strategies.jsx`, `frontend/src/pages/Orders.jsx`, `frontend/src/pages/Positions.jsx`.

**Steps**: add a `reasonLabel(code)` → `{ label, tone, hint }` map (covering Phase 2 + existing codes); render exit_reason / last_filter_reason / rejection_reason through it with a colored chip + tooltip. Keep raw code in a title attr for debugging.

**Verify**: a position closed `theta-decay-12m` shows "Closed early — theta decay (12m)"; a skipped signal `IV_RANK_GATE` shows "Blocked — IV too rich for buying".

### TASK-024 — "Why isn't it trading?" readiness banner
- **Status**: `[x]` commit ff7f411 · 2026-06-16
- **Tier**: 2
- **Session size**: ~1 hour
- **Prerequisite**: None

**Problem**: Token expires nightly + strategies sit paused → screens look silently empty. No in-app signal of the blocking condition.

**Files to touch**: `frontend/src/components/` (new `ReadinessBanner.jsx`), mount in `Layout.jsx` (or Dashboard/Strategies). Reuse existing `/upstox/status` and `/strategies` data.

**Steps**: top strip that shows, when blocking: "Upstox token expired — reconnect" (link to ApiKeys) and "0 of N strategies armed — arm to trade". Hide when token live AND ≥1 strategy live. Subtle, dismissible.

**Verify**: with token disconnected / all paused, banner shows both messages; after reconnect + arming, it disappears.

### TASK-025 — Greeks (δ/θ/IV) on positions & signals
- **Status**: `[x]` commit b8f7f70 · 2026-06-16
- **Tier**: 2
- **Session size**: ~1 hour
- **Prerequisite**: None

**Problem**: `greeks_at_signal`/`greeks_at_entry` now populate but are shown nowhere. Delta-selection picks strikes by δ — users can't see it.

**Files to touch**: `backend/execution_state.py` (carry `delta/theta/iv` + `target_delta` onto the position snapshot), `frontend/src/pages/Positions.jsx` (show δ/θ/IV, ideally a detail popover).

**Steps**: surface entry greeks on the position row/popover; show "δ 0.46 (target 0.45)" when `target_delta` present.

**Verify**: an option position shows its δ/θ/IV; a delta-selected one shows target vs actual.

### TASK-026 — Spread leg detail (expandable row / popover)
- **Status**: `[x]` commit 362f731 · 2026-06-16
- **Tier**: 2
- **Session size**: ~1 hour
- **Prerequisite**: TASK-025 (shares the popover pattern)

**Problem**: Spread positions show a badge + credit/max-loss but not the two legs.

**Files to touch**: `frontend/src/pages/Positions.jsx` (legs already in snapshot from `execution_state.py`).

**Steps**: expandable row/tooltip showing short & long leg (strike/type/premium), net δ/θ, current spread value vs TP(50%)/SL(2×) levels; small progress bar from credit→max-loss.

**Verify**: a credit_spread position expands to show both legs and where value sits between TP and SL.

### TASK-027 — Phase 2 feature-status panel (read-only)
- **Status**: `[x]` commit f872522 · 2026-06-16
- **Tier**: 2
- **Session size**: ~1 hour
- **Prerequisite**: None

**Problem**: Gate flags are env-driven and invisible in-app; you must SSH to know what's on.

**Files to touch**: `backend/server.py` or `backend/routes/ops.py` (new `GET /ops/feature-flags` returning each Phase 2 flag's state), `frontend/src/pages/OpsConsole.jsx` (render a status panel).

**Steps**: endpoint returns theta-exit/delta/IV-rank/order-flow/credit-spreads as ON/OFF/shadow (read env). UI shows colored chips. Read-only (no toggles).

**Verify**: panel matches `docker exec quantg-backend printenv | grep` for the flags.

### TASK-028 — Structure badge on strategy cards
- **Status**: `[x]` commit 63b9829 · 2026-06-16
- **Tier**: 3 (small)
- **Session size**: ~30 min
- **Prerequisite**: None

**Problem**: Can't tell at a glance which option strategies are credit-spread vs single-leg.

**Files to touch**: `frontend/src/pages/Strategies.jsx`.

**Steps**: show a "Spread" / "Single-leg" chip on option strategies from `visual_config.options.structure`.

**Verify**: a strategy set to `credit_spread` shows the Spread chip.

### TASK-029 — IV-Regime card → visual gauge
- **Status**: `[x]` commit 6c15010 · 2026-06-16
- **Tier**: 3 (small)
- **Session size**: ~30 min
- **Prerequisite**: None

**Problem**: MarketHub IV-Regime card is text rows; no visual sense of where IV rank sits.

**Files to touch**: `frontend/src/pages/MarketHub.jsx`.

**Steps**: add a horizontal bar showing IV rank within the 52w min–max, plus a cheap/rich colored chip from `would_block_buys`.

**Verify**: card shows a bar with the marker at the current rank.

### TASK-030 — Per-strategy trade-cap indicator
- **Status**: `[x]` commit 63b9829 · 2026-06-16
- **Tier**: 3 (small)
- **Session size**: ~30 min
- **Prerequisite**: None

**Problem**: New caps (now 8/day) and throttling aren't visible as they fill.

**Files to touch**: `frontend/src/pages/Strategies.jsx` (uses `order_count_today` + `visual_config.risk.max_trades_day`).

**Steps**: small "N / 8 today" counter per strategy; muted when 0, warn-tone when at cap.

**Verify**: a strategy with 3 fills shows "3 / 8".

---

## PRIORITY 7 — Architecture Redesign Stage 0 (Event Catalog + Ownership Map)

Context: CLAUDE.md §11 is now the active architecture program. Stage 0 is documentation only: no runtime code, no fill/P&L/wallet math changes, no new infra, no deployment. The goal is to make the event-bus redesign concrete enough for founder approval before Stage 1.

### TASK-031 — Stage 0A: Create event catalog draft
- **Status**: `[x]` commit this session
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: Founder approval to start Stage 0 task breakdown

**Problem**:
The architecture map identifies the need for an event bus, but the actual events are not named or cataloged. Without a catalog, agents may invent incompatible event names and payloads.

**Files to touch**: Create `docs/architecture/EVENT_CATALOG.md` only.

**Exact steps**:
1. Read AGENTS.md, TASKS.md, and CLAUDE.md §11 first.
2. Inspect the current trade lifecycle only enough to document existing transitions; do not edit code.
3. Draft event families for the current monolith:
   - Strategy lifecycle events
   - Signal lifecycle events
   - Order lifecycle events
   - Fill lifecycle events
   - Position lifecycle events
   - Risk/readiness events
   - Broker/feed events
   - P&L/reporting events
4. For each event, document:
   - Event name
   - Producer today
   - Consumers today
   - Current collection writes caused by the transition
   - Proposed owner module
   - Idempotency key or natural dedupe key
   - Required correlation/causation fields, marked TBD where founder decision is needed
5. Add a "Founder decisions required" section for event naming style, payload schema style, and correlation id format.

**How to verify**:
```bash
git diff -- docs/architecture/EVENT_CATALOG.md
```
Confirm the file is documentation only and contains no implementation instructions that change fill, P&L, wallet, or live-trading behavior.

**Commit format**:
```
docs: draft Stage 0 event catalog for architecture redesign

Task: TASK-031
Tier: 2
Files changed: docs/architecture/EVENT_CATALOG.md
```

---

### TASK-032 — Stage 0B: Create collection ownership map
- **Status**: `[x]` commit this session
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-031

**Problem**:
CLAUDE.md §11 identifies `strategy_positions` and `strategies.today_pnl` as the highest-risk multi-writer zones. The app needs an explicit ownership map before any single-writer refactor starts.

**Files to touch**: Create `docs/architecture/COLLECTION_OWNERSHIP.md` only.

**Exact steps**:
1. Read CLAUDE.md §11.4 and §11.8.
2. Document every core collection currently named in CLAUDE.md §7 and §11:
   - Current purpose
   - Current runtime writers
   - Current readers, where obvious
   - Risk level
   - Proposed single owner
   - Allowed readers
   - Open founder decision, if ownership is contested
3. Mark these as contested until founder approval:
   - `strategy_positions`
   - `strategies.today_pnl`
   - `positions` UI mirror
   - SQLite `option_state_ledger`
4. Explicitly document that `trade_fills` and `paper_wallets` are the safe templates for single ownership.

**How to verify**:
```bash
git diff -- docs/architecture/COLLECTION_OWNERSHIP.md
```
Confirm this task does not change runtime code or database schema.

**Commit format**:
```
docs: map collection ownership for Stage 0 architecture redesign

Task: TASK-032
Tier: 2
Files changed: docs/architecture/COLLECTION_OWNERSHIP.md
```

---

### TASK-033 — Stage 0C: Founder approval memo for Stage 1
- **Status**: `[x]` commit this session
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~1 hour
- **Prerequisite**: TASK-031, TASK-032

**Problem**:
Stage 1 introduces the first in-process event bus and correlation ids. AGENTS.md says event names, payload schemas, correlation id format, and contested owners are stop-and-ask decisions.

**Files to touch**: Create `docs/architecture/STAGE_1_APPROVAL_MEMO.md` only.

**Exact steps**:
1. Summarize the Stage 0 event catalog and ownership map.
2. List the exact founder decisions required before Stage 1:
   - Event naming convention
   - Payload schema convention
   - Correlation id and causation id format
   - First loop to convert
   - Single owner for `strategy_positions`
   - Single owner for `strategies.today_pnl`
   - Deprecate vs delete stance for legacy fill path, `_mongo_position_monitor_loop`, and SQLite `option_state_ledger`
3. Recommend the lowest-risk Stage 1 slice, but do not implement it.
4. Include a clear "Do not deploy" note: Stage 0 is docs-only and does not require VPS deployment.

**How to verify**:
```bash
git diff -- docs/architecture/STAGE_1_APPROVAL_MEMO.md
```
Confirm the memo is readable by the founder without opening code.

**Commit format**:
```
docs: add Stage 1 approval memo after architecture Stage 0

Task: TASK-033
Tier: 2
Files changed: docs/architecture/STAGE_1_APPROVAL_MEMO.md
```

---

## PRIORITY 8 — Architecture Redesign Stage 1 (Publish-Only Event Bus)

Context: Founder approved Stage 1A defaults on 2026-06-18: UPPER_SNAKE_CASE event names, Pydantic payload contracts, `corr:<signal_id>` correlation ids, causation id as previous event id or source record id, existing Mongo `core_events` storage, and signal manager as the first publish-only slice.

### TASK-034 — Stage 1A: Publish-only signal lifecycle events
- **Status**: `[x]` Completed by Codex
- **Commit**: `cebefca`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-033 and founder approval

**Problem**:
Signals move through `PENDING`, `FILTERED`, `SKIPPED_SIGNAL`, and `PROCESSED`, but there is no structured event trail tying the transitions together. Debugging "why didn't it trade?" still depends on mutable signal rows and logs.

**Files to touch**: `backend/core/event_store.py`, `backend/signal_manager.py`, tests under `backend/tests/` if needed.

**Exact steps**:
1. Add a small Pydantic-backed signal event publishing helper using existing Mongo `core_events`.
2. Publish events without changing existing signal/order behavior:
   - `SIGNAL_QUEUED` when the signal manager observes a pending signal for processing
   - `SIGNAL_VALIDATION_FAILED` when validation/limits/quality filtering sets `FILTERED`
   - `SIGNAL_PRIORITY_SKIPPED` when conflict resolution sets `SKIPPED_SIGNAL`
   - `SIGNAL_PROCESSED` when dispatch succeeds and links an order id
   - `SIGNAL_EXECUTION_SKIPPED` when dispatch returns a skipped signal or raises at the execution boundary
3. Use correlation id `corr:<signal_id>`.
4. Use causation id `record:signals:<signal_id>` unless a previous event id is available.
5. Make event persistence best-effort: failure to write `core_events` must never block signal processing.
6. Do not change order creation, fills, wallet, P&L, positions, broker behavior, or live flags.

**How to verify**:
```bash
cd backend
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_audit_fixes.py -v
python -m py_compile core/event_store.py signal_manager.py
```

**Commit format**:
```
feat: publish signal lifecycle audit events without changing trading behavior

Task: TASK-034
Tier: 2
Files changed: backend/core/event_store.py, backend/signal_manager.py, backend/tests/<test-file-if-added>
```

---

### TASK-035 — Extract market routes from server.py into routes/market.py
- **Status**: `[x]` Completed by Codex
- **Commit**: `116d7d8`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-034

**Problem**:
`server.py` still owns market-data and option-preview HTTP endpoints even after earlier route extraction. This keeps market UI/API work tied to the giant startup file.

**Files to touch**: Create `backend/routes/market.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only market/query endpoints into `backend/routes/market.py`:
   - `/market/watchlist`
   - `/market/iv-rank`
   - `/market/candles/{instrument_key:path}`
   - `/market/analytics/option-chain`
   - `/market/analytics/expiry-dates`
   - `/market/commodities`
   - `/market/quote/{symbol}`
   - `/market/feed-comparison`
   - `/market/auto-data-broker`
   - `/market/indicators/{symbol}`
   - `/market/session-status`
   - `/market/session`
   - `/market/regime`
   - `/option-chain/{underlying}`
   - `/options/preview`
2. Register the new market router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, trading execution, broker execution, wallet, P&L, positions, or live flags.
4. Keep `server.py` as the startup authority.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/market.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract market routes from server.py

Task: TASK-035
Tier: 2
Files changed: backend/server.py, backend/routes/market.py, TASKS.md
```

---

### TASK-036 — Extract broker and Upstox routes from server.py
- **Status**: `[x]` Completed by Codex
- **Commit**: `5287022`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~3 hours
- **Prerequisite**: TASK-035

**Problem**:
`server.py` still owns broker key, Upstox OAuth/status/control, Upstox quality, gateway, webhook, and legacy Zerodha HTTP routes. This keeps broker UI/API work tied to the giant startup file.

**Files to touch**: Create `backend/routes/broker.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only broker/upstox/gateway HTTP endpoints into `backend/routes/broker.py`:
   - `/broker/keys`
   - `/broker/keys/{key_id}`
   - `/upstox/data-health`
   - `/upstox/instruments/sync`
   - `/upstox/quality-system/migrate`
   - `/upstox/option-chain`
   - `/upstox/webhook`
   - `/upstox/reconciliation`
   - `/upstox/exit-all`
   - `/zerodha/login-url`
   - `/zerodha/exchange`
   - `/zerodha/status`
   - `/zerodha/disconnect`
   - `/broker/upstox/config`
   - `/broker/upstox/login`
   - `/broker/upstox/callback`
   - `/broker/upstox/order/test`
   - `/broker/upstox/positions`
   - `/broker/upstox/orders`
   - `/broker/upstox/quote`
   - `/broker/upstox/market-data/start`
   - `/upstox/status`
   - `/broker/upstox/status`
   - `/brokers/status`
   - `/diagnostics/health`
   - `/broker/health`
   - `/gateway/check-all`
   - `/gateway/status`
   - `/webhook/upstox/token/{user_id}`
2. Register the new broker router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, broker execution behavior, wallet, P&L, positions, or live flags.
4. Keep `server.py` as the startup authority and keep Upstox feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/broker.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract broker and upstox routes from server.py

Task: TASK-036
Tier: 2
Files changed: backend/server.py, backend/routes/broker.py, TASKS.md
```

---

### TASK-037 — Extract profile, portfolio, funds, and paper wallet routes
- **Status**: `[x]` Completed by Codex
- **Commit**: `60f0678`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-036

**Problem**:
`server.py` still owns profile/account, portfolio/funds, paper-wallet, and paper recovery/reset HTTP routes. These are user/account surfaces and should not stay coupled to the giant startup file.

**Files to touch**: Create `backend/routes/profile.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only account/profile and wallet query/recovery endpoints into `backend/routes/profile.py`:
   - `/portfolio/holdings`
   - `/portfolio`
   - `/funds`
   - `/profile/paper-trading-stats`
   - `/profile`
   - `/profile/reset-paper`
   - `/profile/recover-paper-contract-halts`
   - `/paper-wallet`
   - `/profile/change-password`
2. Register the new profile router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, wallet math, P&L, positions, broker behavior, or live flags.
4. Keep `server.py` as the startup authority and keep execution/feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/profile.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_audit_fixes.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract profile and wallet routes from server.py

Task: TASK-037
Tier: 2
Files changed: backend/server.py, backend/routes/profile.py, TASKS.md
```

---

### TASK-038 — Extract readiness and core health routes from server.py
- **Status**: `[~]` Codex
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-037

**Problem**:
`server.py` still owns readiness and health endpoints even though they are mostly read-only API surfaces. Keeping them inline makes status/readiness work depend on the startup file.

**Files to touch**: Create `backend/routes/readiness.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only readiness/status endpoints into `backend/routes/readiness.py`:
   - `/strategy-readiness`
   - `/paper-readiness`
   - `/live/readiness`
   - `/trading/live-readiness`
   - `/core/live/readiness`
   - `/core/health`
   - `/core/market-status`
   - `/core/feed-status`
2. Register the new readiness router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, readiness logic, live flags, broker behavior, wallet, P&L, or positions.
4. Keep `server.py` as the startup authority and keep execution/feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/readiness.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract readiness routes from server.py

Task: TASK-038
Tier: 2
Files changed: backend/server.py, backend/routes/readiness.py, TASKS.md
```

---

### TASK-039 — Extract remaining ops runtime routes from server.py
- **Status**: `[ ]`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-038

**Problem**:
`server.py` still owns a few operational HTTP routes. Moving them into a small ops-runtime module reduces route coupling without touching startup loops.

**Files to touch**: Create `backend/routes/ops_runtime.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only these ops endpoints into `backend/routes/ops_runtime.py`:
   - `/ops/v12/upstox-retailer/activate`
   - `/ops/squareoff-all`
   - `/ops/trading-ready`
2. Register the new ops-runtime router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, order behavior, live flags, wallet, P&L, or positions.
4. Keep `server.py` as the startup authority and keep execution/feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/ops_runtime.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_audit_fixes.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract ops runtime routes from server.py

Task: TASK-039
Tier: 2
Files changed: backend/server.py, backend/routes/ops_runtime.py, TASKS.md
```

---

### TASK-040 — Extract core data and backtest routes from server.py
- **Status**: `[ ]`
- **Tier**: 2 (Codex / Sonnet / GPT-4o)
- **Session size**: ~2 hours
- **Prerequisite**: TASK-039

**Problem**:
`server.py` still owns core read-only data routes and the core backtest runner endpoint. These should live beside the other route modules so the startup file can keep shrinking.

**Files to touch**: Create `backend/routes/core_status.py`. Edit `backend/server.py` and `TASKS.md`.

**Exact steps**:
1. Move only these core data/backtest endpoints into `backend/routes/core_status.py`:
   - `/core/strategies`
   - `/core/orders`
   - `/core/positions`
   - `/core/performance`
   - `/core/backtests`
   - `/core/backtests/run`
   - `/core/live/arm`
   - `/core/live/disarm`
   - `/core/kill-switch`
2. Register the new core-status router in `server.py`.
3. Do not change endpoint URLs, auth, request parameters, response shapes, order behavior, live flags, wallet, P&L, or positions.
4. Keep `server.py` as the startup authority and keep execution/feed/runtime wiring there.

**How to verify**:
```bash
cd backend
python -m py_compile server.py routes/core_status.py
python -m pytest tests/test_core_logic.py -v
python -m pytest tests/test_audit_fixes.py -v
python -m pytest tests/test_signal_events.py -v
```

**Commit format**:
```
refactor: extract core status routes from server.py

Task: TASK-040
Tier: 2
Files changed: backend/server.py, backend/routes/core_status.py, TASKS.md
```

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
| TASK-021 | Implement live readiness and paper trading audit fixes | b38e5c0 | 2026-06-15 |
| TASK-022 | Redesign Strategies UI, relocate test action to About modal | 43d4dd9 | 2026-06-15 |
| TASK-023 | Friendly labels for Phase 2 exit/skip reasons | 86705ba | 2026-06-16 |
| TASK-024 | "Why isn't it trading?" readiness banner | ff7f411 | 2026-06-16 |
| TASK-025 | Greeks (δ/θ/IV) on positions | b8f7f70 | 2026-06-16 |
| TASK-026 | Expandable spread leg detail on Positions | 362f731 | 2026-06-16 |
| TASK-027 | Phase 2 feature-flag status panel | f872522 | 2026-06-16 |
| TASK-028 | Structure badge on strategy cards | 63b9829 | 2026-06-16 |
| TASK-029 | IV-regime visual gauge | 6c15010 | 2026-06-16 |
| TASK-030 | Per-strategy trade-cap indicator | 63b9829 | 2026-06-16 |

---

*Last updated: 2026-06-18*
*Total tasks: 40*
*Open: 2 · In progress: 1 · Done: 37*
