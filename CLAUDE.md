# QuantG — AI Agent Operating Manual

**Read this first. Every session. No exceptions.**

This is the canonical reference for all AI agents working on the QuantG algo-trading platform. It supersedes all other documentation.

---

## 1. What This App Is

QuantG is an NSE/BSE options algo-trading platform.
- **Backend**: FastAPI + Motor (MongoDB) + asyncio loops. Single file: `backend/server.py` (~15k lines). Additional routes in `backend/routes/`.
- **Frontend**: React (CRA) + Tailwind. Modular architecture: pages in `frontend/src/pages/`, presenter components in `frontend/src/components/`, centralized state in `frontend/src/contexts/`. No SSR.
- **Infra**: Docker Compose on VPS. Four containers: `quantg-backend`, `quantg-frontend`, `quantg-mongo`, `quantg-caddy`.
- **Broker**: Upstox (V3 WebSocket feed + REST orders). No Zerodha, no MCX, no Kite.
- **Domain**: quantgtrade.com → VPS 82.180.145.183

### Current Mode
Paper trading (PAPER). Live trading infra exists but `CORE_ENGINE_LIVE_ENABLED=false` in docker-compose.yml.

---

## 2. Critical File Map

| What you want to change | File |
|---|---|
| API route logic (orders, signals, strategies) | `backend/server.py` |
| Ops routes (enable-all, emergency-stop, reconciliation) | `backend/routes/ops.py` |
| Auth routes | `backend/routes/auth.py` |
| AI bot routes | `backend/routes/ai.py` |
| Pre-trade risk checks (Greeks, kill-switch, sizing) | `backend/core/risk_manager.py` |
| Position ledger (create/close positions from fills) | `backend/core/portfolio_ledger.py` |
| Order routing (paper vs live) | `backend/core/execution_router.py` |
| Idempotency key generation | `backend/core/order_manager.py` |
| Market domains (lot sizes, NSE/BSE segments) | `backend/core/market_domains.py` |
| Strike/contract selection | `backend/core/option_selector_v2.py` |
| Paper fill simulation | `backend/core/paper_broker.py` |
| Pure risk / exit-reason functions (no DB) | `backend/core/position_lifecycle.py` |
| Position monitor loop (extracted from server.py) | `backend/position_monitor.py` |
| Execution snapshot (positions+orders for UI polling) | `backend/execution_state.py` |
| Upstox WebSocket V3 feed + REST orders | `backend/brokers/upstox_gateway.py` |
| Signal processing loop | `backend/signal_manager.py` |
| Strategy evaluation loop | `backend/strategy_runner.py` |
| Broker reconciliation (live only) | `backend/position_reconciler.py` |
| Frontend positions page | `frontend/src/pages/Positions.jsx` |
| Frontend orders page | `frontend/src/pages/Orders.jsx` |
| Frontend strategies page | `frontend/src/pages/Strategies.jsx` |
| Frontend ops/risk page | `frontend/src/pages/OpsConsole.jsx` |
| Frontend dashboard page | `frontend/src/pages/Dashboard.jsx` |
| Frontend wiki / knowledge hub | `frontend/src/pages/Wiki.jsx` |
| Frontend AI bot page | `frontend/src/pages/AIBot.jsx` |
| Frontend market hub page | `frontend/src/pages/MarketHub.jsx` |
| Frontend global state provider | `frontend/src/contexts/ExecutionStateContext.jsx` |
| Frontend execution data hook | `frontend/src/hooks/useExecutionState.js` |
| Frontend dashboard components | `frontend/src/components/dashboard/` (KpiCard, NiftyPulseChart, StrategyPerformanceTable, StrategyLedgerRow, HealthScoreList, AllocationList) |
| Frontend strategy components | `frontend/src/components/strategies/` (StrategyCard, RuntimeSettingsForm, AboutStrategyModal) |
| Frontend ops components | `frontend/src/components/ops/` (MarginMeterPanel, UserReconciler, BrokerStatusPanel, IncidentRecoveryPanel, OpsActionCard) |
| Frontend wiki components | `frontend/src/components/wiki/` (MarkdownRenderer, PhysicsGraphCanvas, WikiTreeSidebar) |
| Frontend AI bot components | `frontend/src/components/aibot/` (ChatFeed, SessionHistorySidebar, PromptSuggestionsPanel) |
| Frontend app shell / layout | `frontend/src/components/Layout.jsx`, `frontend/src/components/ui/` |
| Frontend CSS design system | `frontend/src/index.css` (HSL variables, Sora/Inter/Mono fonts, blink keyframe animations) |
| Docker build/env config | `docker-compose.yml`, `backend/.env` |
| Reverse proxy config | `Caddyfile` |

---

## 3. Deployment Procedure

**Always follow this order. Never skip steps.**

### Step 1: Make and commit changes locally
```powershell
# From d:\Quant\QuantG
git add <specific files>   # never git add -A — could commit secrets
git commit -m "describe the fix"
```

### Step 2: Push to GitHub
```powershell
git push origin main
```

### Step 3: Pull on VPS
```bash
ssh -i C:\Users\MG\.ssh\codex_quantg_vps root@82.180.145.183
cd /opt/QuantG && git pull origin main
```

### Step 4: Restart containers

**Backend-only change** (Python, routes):
```bash
# IMPORTANT: restart alone does NOT reload Python code — must rebuild first
docker-compose build backend && docker-compose up -d backend
```

**Frontend change** (JSX, CSS — requires rebuild):
```bash
docker-compose build frontend && docker-compose up -d frontend
```

**Both changed**:
```bash
docker-compose build frontend && docker-compose restart backend && docker-compose up -d frontend
```

**Full rebuild** (docker-compose.yml, Dockerfile, dependencies):
```bash
docker-compose build --no-cache && docker-compose up -d
```

### Step 5: Verify
```bash
docker-compose logs backend --tail=30
# Expect: no ERROR lines, Upstox feed connected, startup tasks complete
```

### One-liner for backend-only deploys (from Windows):
```powershell
cd "d:\Quant\QuantG"
git add <files> && git commit -m "fix: ..." && git push origin main
ssh -i "C:\Users\MG\.ssh\codex_quantg_vps" root@82.180.145.183 "cd /opt/QuantG && git pull origin main && docker-compose build backend && docker-compose up -d backend"
```

---

## 4. Key Technical Concepts

### Position Lifecycle
```
RESERVED → PENDING_OPEN → PENDING_BROKER → OPEN → FILLED → EXITING → CLOSED
```
- Monitor loop (`position_monitor.run_monitor_loop`) queries only `{PENDING_BROKER, OPEN, FILLED}` — not EXITING/CLOSED. Runs every 30 s. Positions stuck in EXITING > 5 min are auto-reverted to OPEN.
- Always mark position EXITING atomically **before** placing exit order. Revert to OPEN on failure.
- Exit circuit breaker: after 3 failed exit attempts the position moves to `CIRCUIT_BREAKER` status and is skipped until manually resolved.

### Idempotency Keys
- **Entry orders**: `sha256(strategy_id:domain:symbol:side:date:HH:MM)[:32]` — minute-granular, prevents same-candle double-fire
- **Exit orders**: `"exit:{pos_id}:{reason[:20]}"` — position-specific, prevents duplicate exits
- Never reuse an entry idempotency key for exits.

### Upstox V3 WebSocket Format
- Instrument keys: `"NSE_FO|<NUMERIC_TOKEN>"` (e.g. `"NSE_FO|42285"`)
- Index keys: `"NSE_INDEX|Nifty 50"`, `"BSE_INDEX|SENSEX"`
- Token subscription is done via `gateway.start_market_data_ws(tokens, mode="ltpc")`
- Tokens are re-subscribed on server restart via `_subscribe_open_position_tokens_on_startup` (8s delay)

### Options Symbol Format
NSE verbose format: `"NIFTY 23200 CE 09 JUN 26"` — **contains spaces, does NOT end with "CE"/"PE"**.
Always use `"CE" in symbol` (not `symbol.endswith("CE")`).

### Greeks Delta Proxy
`_check_greeks_exposure` uses a 0.5 ATM delta approximation:
- CE long = +0.5×qty delta, CE short = −0.5×qty delta
- PE long = −0.5×qty delta, PE short = +0.5×qty delta
- Exit/reduction orders always bypass the delta cap.

### Lot Sizes
- NIFTY: 65, BANKNIFTY: 30 (source of truth: `core/market_domains.py`)
- Always resolve via `resolve_domain_by_underlying(underlying).get_lot_size(underlying)`, not hardcoded.

### Paper Wallet
- Balance starts at ₹500,000. Lives in `db.paper_wallets`.
- Over-crediting happens if duplicate exit orders fill — each fill credits the wallet separately.

### User Roles
- `owner` — platform admin. Cross-account operations (approve users, full reset).
- `trader` — normal user. Can do everything on their own account.
- Role checks should only block cross-account or destructive-all-accounts operations.

### Core Engine Flags (docker-compose.yml environment)
```
CORE_ENGINE_ENABLED=true           # must be true for all trading
CORE_ENGINE_PAPER_ENABLED=true     # enables paper order execution
CORE_ENGINE_LIVE_ENABLED=false     # set true only for live trading
```

---

## 5. Bug Fix Workflow

### Before writing any code:
1. **Read the VPS logs first**: `docker-compose logs backend --tail=100 2>&1 | grep -i "error\|exception\|blocked\|reject"` — the error message usually names the exact file and function.
2. **Identify the exact file** using the File Map above. Do not grep the entire `.venv` folder.
3. **Read the relevant function** before editing — understand the full flow.

### Finding code:
```bash
# Find an endpoint:
grep -n "@api.post\|@api.get" backend/server.py | grep "keyword"

# Find an ops route:
grep -n "def ops_" backend/routes/ops.py

# Find where an error comes from:
grep -n "error message text" backend/server.py backend/routes/*.py backend/core/*.py
```

### Making changes:
- Edit the **smallest possible scope** — one function, one file.
- Never add broad error handling or feature flags unless required.
- Test by restarting backend and checking logs before declaring done.

### After changes:
- Always restart backend (minimum) and tail logs to confirm clean startup.
- For frontend changes, always rebuild the Docker image — `docker-compose restart frontend` does NOT pick up JSX changes.

---

## 6. Common Pitfalls (learned the hard way)

| Pitfall | Correct Approach |
|---|---|
| `symbol.endswith("CE")` for options | `"CE" in symbol` |
| `pos.get("side")` for position direction | `pos.get("position_side")` (values: "LONG"/"SHORT") |
| Hardcoding `lot_size=1` for options exit | `resolve_domain_by_underlying(underlying).get_lot_size(underlying)` |
| Using `pos["average_price"]` directly | `pos.get("average_price") or pos.get("average_buy_price") or 0` |
| `exit_qty = qty` for any exit order | `exit_qty = pos.get("open_quantity")` — use what's open, not what was ordered |
| Position monitor retrying EXITING positions | Atomically set status=EXITING before placing exit; revert to OPEN on failure |
| Grepping backend/ folder includes .venv | Always add `--include="*.py"` and exclude `.venv` |
| Restarting frontend container for UI changes | `docker-compose build frontend && docker-compose up -d frontend` |
| Owner-role check on per-user account ops | Only use owner check for cross-account operations (user approval, full reset) |
| Creating phantom SHORT from duplicate SELL fills | Check for CLOSED/EXITING LONG before creating new position from a SELL fill |

---

## 7. Database Collections (MongoDB `quantg`)

| Collection | Purpose |
|---|---|
| `users` | User accounts, roles, settings, paper_mode flag |
| `strategies` | Strategy definitions, status (live/paused/draft), today_pnl |
| `orders` | All orders (paper + live), idempotency keys |
| `strategy_positions` | Open/closed option positions per strategy |
| `positions` | Simplified position mirror for UI (paper mode) |
| `paper_wallets` | Paper trading balance per user |
| `signals` | Strategy signals (PROCESSED, FILTERED, SKIPPED) |
| `broker_keys` | Upstox access tokens per user |
| `live_arm_state` | Live trading arm state per user |
| `risk_state` | Kill switch, reconciliation state |
| `risk_reservations` | Pre-order capital reservations |
| `trade_fills` | Fill records from paper broker / Upstox callbacks |
| `processed_fill_ids` | Unique index on `fill_id` — prevents duplicate fill processing (DB-level idempotency) |

---

## 8. Credentials & Access

| Resource | Value |
|---|---|
| VPS IP | 82.180.145.183 |
| VPS SSH key | `C:\Users\MG\.ssh\codex_quantg_vps` |
| VPS repo path | `/opt/QuantG` |
| Production domain | https://quantgtrade.com |
| GitHub remote | `origin` (soni121gs/QuantG) |
| MongoDB | `mongodb://mongo:27017` (internal Docker network) |

---

## 9. What NOT to Do

- **Do not** add `import` statements inside functions unless required to avoid circular imports.
- **Do not** create new markdown documentation files. Update CLAUDE.md or DEPLOY.md instead.
- **Do not** commit `.env` files, `runtime_state.sqlite3`, or `quantg.log`.
- **Do not** `git add -A` — always stage specific files.
- **Do not** `docker-compose restart frontend` to deploy UI changes — it doesn't rebuild.
- **Do not** touch `backend/core_legacy.py` — it's kept for rollback reference only.
- **Do not** enable `CORE_ENGINE_LIVE_ENABLED=true` unless explicitly instructed.
- **Do not** delete MongoDB volumes or run `docker-compose down -v` — it wipes all user data.
- **Do not** create scratch scripts in the backend root — use `backend/scratch/` or run inline.

---

## 10. QuantG Company Roadmap — Vision & Growth Plan

**Last updated: June 2026**

This is the founder's long-term vision for QuantG. Every AI agent working on this codebase must understand this context. Features, priorities, and architectural decisions should be evaluated against this roadmap.

---

### The North Star

> QuantG's goal is to evolve from a personal algo-trading platform into a full-scale proprietary quantitative trading firm — and eventually a licensed fund manager (PMS/AIF) that manages external capital. The platform infrastructure (execution engine, risk manager, position lifecycle, signal processor) is already built. The mission now is to prove alpha with real capital, compound it, and scale.

The template is **Graviton Research Capital**: two engineers, $1M seed capital in 2014, India's first HFT unicorn ($1.3B) by 2026. QuantG starts with better infrastructure than Graviton had on day one.

---

### Phase 1 — Prove Alpha with Real Capital
**Timeline: Now → Month 12**
**Capital needed: ₹5L–25L (personal)**

#### Goals
- Enable `CORE_ENGINE_LIVE_ENABLED=true` in docker-compose.yml (founder's decision — not done by AI agents)
- Deploy 2–3 live strategies simultaneously on NIFTY/BANKNIFTY options
- Build a verified 12-month live P&L track record
- Target performance metrics:
  - Monthly return: 3–5%
  - Sharpe Ratio: > 1.5
  - Max Drawdown: < 20%
  - Win rate: > 55%

#### Platform priorities in this phase
- Strategy performance analytics dashboard (per-strategy P&L, drawdown charts)
- Live P&L export (for investor presentation / audit trail)
- Multi-strategy simultaneous execution (already supported — needs stress testing)
- Position sizing logic based on capital allocation %

#### What NOT to build in this phase
- Subscription/multi-user onboarding features
- PMS/AIF client reporting
- Any public-facing marketing pages

---

### Phase 2 — Scale Capital + Informal HNI Co-investment
**Timeline: Month 12 → Month 24**
**Capital target: ₹50L–2Cr (via Trading LLP)**

#### Goals
- Form a **Trading LLP** (all parties are partners — no SEBI license needed)
- Bring in 2–5 HNI co-investors (ex-traders, finance professionals, family offices)
- Present verified live P&L track record as the pitch
- Target Rainmatter (Zerodha's fund) for a seed investment of ₹50L–2Cr
  - Rainmatter invests ₹50L–₹100Cr range, no stage gate, fintech-focused
  - They backed Sensibull (options platform) and Capitalmind (quant PMS) — QuantG fits their mandate
- Scale deployed capital to ₹50L–1Cr across 5–8 strategies

#### Platform priorities in this phase
- Multi-user LLP partner dashboard (each partner sees their P&L share)
- Automated monthly P&L reports (PDF export, email)
- Strategy backtesting module (validate new strategies before going live)
- FINNIFTY and MIDCPNIFTY support (beyond NIFTY/BANKNIFTY)
- Risk limit configuration per strategy (capital %, max loss per day)

#### Funding pitch narrative
"QuantG is a self-hosted quant trading platform with a live 12-month track record of X% returns at Y% max drawdown. The platform has production-grade execution, risk management, and position lifecycle management. We are raising ₹[X]Cr to scale capital deployment across 8 proven strategies on NSE F&O."

---

### Phase 3 — SEBI PMS License + Subscription Platform
**Timeline: Month 24 → Month 36**
**Capital target: ₹10–25Cr AUM**

#### Goals
- Achieve ₹5Cr net worth (required for SEBI PMS registration)
- File for **SEBI Portfolio Management Services (PMS)** license
  - Registration fee: ₹10L | Net worth required: ₹5Cr | Min client ticket: ₹50L
- Begin managing HNI money legally — charge 2% management + 15–20% performance fees
- Launch the **subscription model** for retail traders:
  - Strategy signal alerts (Telegram/app push)
  - Strategy templates for Tradetron/Streak integration
  - Monthly subscription: ₹999–₹4,999/month
- Target AUM: ₹10–25Cr (20–50 HNI clients at ₹50L each)

#### Revenue model at this phase
| Source | Rate | At ₹25Cr AUM |
|---|---|---|
| PMS management fee | 2%/year | ₹50L/year |
| PMS performance fee | 15% above 10% hurdle | ₹75L–₹1.5Cr/year |
| Subscription (500 users × ₹2,000/month) | — | ₹1.2Cr/year |
| **Own prop capital profits** | 3–5%/month on ₹2Cr | ₹72L–₹1.2Cr/year |
| **Total** | | **₹3–4Cr/year** |

#### Platform priorities in this phase
- SEBI-compliant client reporting module (mandatory for PMS)
- Subscriber portal (strategy signals, performance history, subscription management)
- Multi-broker support (add Zerodha Kite, AngelOne — not just Upstox)
- Options market-making module (quoting both sides, delta-hedging — AlphaGrep/Quadeye direction)
- Automated tax P&L statement generation (F&O gains report)

---

### Phase 4 — SEBI Category III AIF (Hedge Fund)
**Timeline: Year 3 → Year 5**
**Capital target: ₹50–200Cr corpus**

#### Goals
- Upgrade legal structure to **SEBI Category III Alternative Investment Fund**
  - Minimum corpus: ₹20Cr | Min per investor: ₹1Cr | Leverage: up to 2x NAV
- Open to domestic HNIs, family offices, NRIs, and institutional investors
- Build quant research team: 2–3 researchers + 2 developers dedicated to alpha generation
- Expand into statistical arbitrage strategies (cash-futures, cross-exchange, calendar spreads)
- Move VPS to **Mumbai co-location** (CtrlS / Tata Communications) for lower latency
- Consider **NSE co-location** (₹22.5L/year) if moving into higher-frequency strategies

#### Revenue model at ₹100Cr AUM (2/20 fee structure)
| Source | Amount |
|---|---|
| Management fee (2%/year) | ₹2Cr/year |
| Performance fee (20% of 25% returns) | ₹5Cr/year |
| Subscription platform | ₹2–3Cr/year |
| Own prop capital (₹10Cr at 40%/year) | ₹4Cr/year |
| **Total** | **₹13–14Cr/year** |

#### Platform priorities in this phase
- AIF investor portal (NAV reporting, audit trail, SEBI compliance)
- Real-time risk dashboard for fund managers (VaR, Greeks exposure, concentration)
- Statistical arbitrage strategy modules
- Latency optimization (binary WebSocket protocols, connection pooling)

---

### Phase 5 — GIFT City AIF + Global Expansion
**Timeline: Year 5 → Year 7**
**Capital target: ₹500Cr+ AUM**

#### Goals
- Set up a **GIFT City (IFSCA) AIF** for:
  - Zero STT (Securities Transaction Tax) — saves crores at scale
  - Zero stamp duty on trades
  - USD-denominated fund — attracts NRI and foreign capital
  - Pass-through taxation (no fund-level tax)
- Attract international capital (Singapore family offices, NRI investors, global HNIs)
- Expand strategy coverage: global indices, crypto derivatives, US options (via Alpaca API)
- Potentially apply for **NSE/BSE membership** (direct market access without broker)
- AUM target: ₹200–500Cr

#### Competitive positioning at this stage
QuantG will be directly competing with:
- **iRage Capital** (India HFT, ~$5.3M revenue — beatable)
- **Lares Algotech** (stat arb, similar scale)
- **Estee Advisors** (quant-driven fund)

NOT competing with (different tier):
- Graviton Research Capital (HFT unicorn, ₹1.6L Cr intraday turnover)
- Jane Street / Citadel Securities (global market makers)

---

### Competitive Advantages QuantG Already Has

| Advantage | Why It Matters |
|---|---|
| Full execution infrastructure built | Graviton spent years on this. QuantG has it on day one. |
| Risk manager with Greeks exposure caps | Institutional-grade — most retail quant startups lack this |
| Paper trading engine | Test strategies with zero risk before live deployment |
| Position lifecycle (RESERVED→CLOSED) | Prevents duplicate fills, phantom positions, over-crediting |
| Idempotency keys on all orders | Bank-grade order deduplication |
| Self-hosted on ₹5K/month VPS | Zero SaaS cost vs ₹10L+/month for cloud infra at scale |
| Upstox V3 WebSocket integration | Real-time tick data — 3–6 months of work most startups spend here |

---

### Key Metrics to Track (Every Session Should Know These)

| Metric | Phase 1 Target | Phase 3 Target | Phase 5 Target |
|---|---|---|---|
| Deployed capital | ₹5–25L | ₹10–25Cr AUM | ₹200–500Cr AUM |
| Monthly return (net) | 3–5% | 2–4% (larger capital) | 1.5–3% |
| Sharpe Ratio | > 1.5 | > 1.8 | > 2.0 |
| Max Drawdown | < 20% | < 15% | < 12% |
| Number of strategies live | 2–3 | 8–15 | 20–40 |
| Annual revenue | ₹0 (reinvest all) | ₹3–4Cr | ₹15–25Cr |
| Team size | 1 (founder) | 3–5 | 15–25 |

---

### Regulatory Milestones

| Milestone | Requirement | When |
|---|---|---|
| Live trading enabled | `CORE_ENGINE_LIVE_ENABLED=true` | Phase 1 |
| Trading LLP formed | Company registration | Phase 2 |
| SEBI algo registration | NSE circular compliance (Aug 2025 mandate) | Phase 2 |
| SEBI PMS license | ₹5Cr net worth, ₹10L fee | Phase 3 |
| SEBI Cat III AIF | ₹20Cr corpus, ₹1Cr/investor | Phase 4 |
| GIFT City IFSCA AIF | IFSCA registration | Phase 5 |
| NSE/BSE direct membership | Exchange membership fees + capital | Phase 5 |

---

### What the Platform Needs to Become (Architecture North Star)

The current QuantG platform is a **personal trading terminal**. The roadmap requires it to evolve into a **multi-strategy fund management system**. Future architectural additions (in order of priority):

1. **Strategy backtesting engine** — historical NSE options chain data, walk-forward testing
2. **Performance analytics** — per-strategy Sharpe, drawdown charts, monthly P&L reports
3. **Multi-broker layer** — abstract execution_router to support Zerodha Kite + AngelOne beyond Upstox
4. **Options market-making module** — quote both sides, manage delta exposure automatically
5. **Subscriber portal** — for Phase 3 subscription business
6. **AIF/PMS client reporting** — SEBI-compliant NAV reports, audit trail, investor dashboard
7. **Stat arb strategies** — cash-futures arbitrage, calendar spread automation
8. **GIFT City integration** — separate execution path for IFSCA-regulated fund

---

## 11. Architecture Evolution — Brain / Event-Bus Redesign (Design Map)

**Status: DESIGN ONLY — nothing below is implemented yet. Captured 2026-06-18.**

This is the complete system map produced from a live read of the codebase. It is the
reference for the planned evolution from a modular monolith to an event-driven,
agent-workable, market-grade platform. **One-sentence thesis: QuantG's problems are not in
its logic — they are in its ownership.** The fix is drawing clear boundaries (one writer per
state slice, one event bus, one event log), not a rewrite.

### 11.1 Current state (as-is, verified)

One Python process, one asyncio loop, fronted by Caddy, backed by Mongo + a parallel SQLite
option ledger. **No message broker, no Redis, no pub/sub exists.** Modules are coupled three
ways, and all three are the problem:

| Coupling today | What it causes |
|---|---|
| Shared Mongo collections ("blackboard") | Multiple writers to one slice → phantom-position / P&L-cache / over-credit bugs |
| Injected callbacks (`place_order_fn`) | Direct point-to-point calls — brittle chains, hard to test in isolation |
| Polling (15s/30s/180s loops, frontend snapshot) | Timer-driven, not event-driven; a fill can wait tens of seconds before anything reacts |

Note the seams already exist: `position_monitor.py` / `position_guardian.py` take injected
deps and refuse to import `server.py`; `trade_fills` and `paper_wallets` are already
single-owned. The architecture is **unfinished, not broken**.

### 11.2 Module inventory (real files, grouped)

- **Edge / API** — `server.py` (18,484 lines), `routes/*`. **151 endpoints total: 80 still inline in `server.py`, 71 extracted.**
- **Loops (9, all one process)** — `strategy_runner`, `signal_manager`, `position_monitor`, `position_guardian`, `position_reconciler` + 4 inline in `server.py` (`_strategy_health_loop`, `_option_engine_monitor_loop`, `_broker_reconciliation_loop`, `_daily_scheduler_loop`).
- **Execution** — `core/execution_router`, `core/paper_broker`, `core/order_manager`, `brokers/upstox_gateway`, `brokers/upstox_market_data_v3`.
- **Risk / sizing** — `core/risk_manager`, `risk_controls`, `core/live_entry_preflight`, `core/readiness_checker`.
- **State / ledger** — `core/portfolio_ledger`, `core/position_lifecycle`, `core/position_manager`, `core/spread_lifecycle`, `option_state_ledger` (SQLite — parallel to Mongo), `execution_state`, `execution_bridge`.
- **Selection / market** — `core/option_selector_v2`, `core/instrument_resolver`, `core/quote_service`, `core/market_domains`, `market_regime`, `market_protection`, `upstox_trading_quality`, `trade_frequency`.
- **Reporting / AI** — `daily_strategy_reporter`, `core/strategy_leaderboard`, `core/backtest_engine`, `backtrader_runner`, `routes/ai`.

### 11.3 Loop cadences (verified, not docs)

| Loop | Cadence | Source |
|---|---|---|
| Signal manager | 2 s | `SIGNAL_MANAGER_TICK_SECONDS=2` |
| Position guardian | 5 s | `GUARDIAN_POLL_SECONDS=5` |
| Daily scheduler | 10 s tick | `server.py` |
| Strategy runner | 15 s | `STRATEGY_RUNNER_TICK_SECONDS=15` |
| Strategy health | 30 s | `server.py` |
| Position monitor | ~30 s in-hours poll | `position_monitor.py` |
| Broker reconciliation | 180 s | `RECONCILIATION_INTERVAL=180` |
| EXITING auto-revert | 300 s | `config.py` |

### 11.4 Collection writer heat map (THE bug-zone evidence)

Runtime writers per core collection (excludes one-off scratch/migration/reset scripts):

| Collection | Writers | Risk |
|---|---|---|
| `strategy_positions` | **6** — server, runner, monitor, guardian, reconciler, spread | 🔴 bug zone |
| `strategies` (`today_pnl` cache) | **6** — server, runner, signal_mgr, trade_freq, reconciler, spread | 🔴 bug zone |
| `orders` | 3 — server, upstox_quality, spread | 🟠 |
| `signals` | 3 — runner inserts, signal_mgr updates, server | 🟠 |
| `positions` (UI mirror) | 2 — server, reconciler | 🟠 |
| `risk_state` | 2 — server, reconciler | 🟠 |
| `trade_fills` | converged — server fill-path, spread (single logical source) | 🟢 safe |
| `paper_wallets` | 1 — `core/paper_broker` only | 🟢 safe |

The two truth-bearing collections are the **least**-owned. `trade_fills` + `paper_wallets`
prove the single-writer cure works — they are the template for the rest.

### 11.5 Trade lifecycle today (each hop writes a collection the next polls)

```
strategy_runner (15s)  → writes signals, strategies
   ↓
signal_manager (2s)    → calls place_order
   ↓
place_order (server.py)→ writes orders, risk_reservations   [idempotency + risk reserve]
   ↓
execution_router → paper_broker → writes paper_wallets       [simulate fill]
   ↓
fill handler + portfolio_ledger → writes trade_fills, positions, strategy_positions
   ↓
position_monitor (~30s) + guardian (5s) → exit → writes strategy_positions
   ↓
execution_state snapshot → UI (polled); P&L derived from trade_fills
```

No event records *who caused what* — the debugging gap and the SEBI audit gap.

### 11.6 Key findings

1. `server.py` (18.5k lines) is the gravity well: 80/151 endpoints, 4/9 loops, the fill/exit engine. Not agent-loadable.
2. Schema sprawl: ~45 collections actually touched (only 13 documented in §7). No registry.
3. The truth-bearing collections (`strategy_positions`, `strategies.today_pnl`) have 6 writers each — structural root of the phantom-P&L bug class.
4. `trade_fills` + `paper_wallets` are single-owned and bug-free — the proof and the template.
5. Hand-rolled DB locks (`runner_locks`, `strategy_position_locks`, `risk_reservation_locks`) exist only to stop writers racing. Single-writer + event bus would delete the need for most of them.
6. Timer-driven, not event-driven: runner 15s → signal 2s → fill → monitor ~30s. Reaction lags.
7. Dual-source remnants: legacy fill engine (fenced), `_mongo_position_monitor_loop` (superseded), SQLite `option_state_ledger` parallel to Mongo `strategy_positions`.

### 11.7 Target blueprint (layered, one deployable process first)

```
External:   Market (Upstox feed/orders)        Users & clients (web, future portal/api)
Edge:       Caddy · auth · multi-tenant · rate-limit
Perceive/   Feed handler · Quote service · Strategy runner · Risk manager   (teal)
decide
====================  EVENT BUS  ====================   (pub/sub; every event carries
                                                         correlation id + causation id)
Act/        Order router · Broker adapters · Position ledger · P&L + reconcile  (purple)
remember
Memory:     MongoDB (system of record) · Redis (working mem + bus bridge) · Event store (log+audit)
Cross-cut:  Observability/tracing · Audit/compliance · Agent workspace
```

It stays a **modular monolith** at first — microservices are explicitly rejected for a
single ₹5K VPS / solo founder. A module is peeled into its own Redis-backed worker only when
a real bottleneck forces it, using the same event contracts.

### 11.8 The five invariants (this IS the design)

1. **Single-writer per state slice.** Only the ledger writes positions; only the P&L engine derives realized P&L from `trade_fills`.
2. **Broadcast freely, mutate narrowly.** Many readers, one writer. Multidimensional *reads*, never multidimensional *writes*.
3. **Every event carries correlation + causation ids** — the replacement for the stack trace you lose going event-driven; also the audit trail.
4. **Idempotency everywhere money moves** (already done on order keys — extend to every consumer).
5. **Paper and live share one execution port** — identical event flow, only the adapter differs.

### 11.9 Agent-workability

Each module becomes a self-contained cell: `handler.py` + `contract.md` (events in/out, state
owned) + `tests/` (replayed-event fixtures) + `manifest.yaml`. An agent loads one cell
(~500 lines), not 18.5k. The event contract is the API across boundaries; the event store
gives deterministic replay for tests; multiple agents work different cells without collision.

### 11.10 Migration ladder (you-are-here → market). Each rung ships independently.

```
[Market-grade fund platform — live · multi-tenant · auditable · agent-ready]   (goal)
 6  Scale-out — Redis workers + new lobes            (roadmap Phase 2-4, only when needed)
 5  Live-trading hardening + audit trail             (roadmap Phase 1, founder gate)
 4  Event store + tracing                            (replay + audit = your stack trace)
 3  Carve server.py into module cells                (agent-sized, thin server.py)
 2  Single-writer ownership per slice                (kills the phantom-money bug class)
 1  In-process event bus + correlation ids           (convert one loop first)
 0  Event catalog + ownership map                    (DOC ONLY · zero risk · START HERE)
[Today — modular monolith · blackboard · ~30s latency]   (you are here)
```

Ordering is deliberate: money-correctness steps (0–2) come before concurrency steps (3–6),
so the race-bug class is fixed before more concurrency is added. **Next concrete step is
Stage 0** (event catalog + collection ownership map) — pure documentation, zero code risk.

---

## 12. Knowledge Hub (Obsidian Wiki & Auto-Memory)

QuantG features a bidirectional Knowledge Hub synchronizing markdown files with MongoDB.

- **Directories**: Subdirectories under `wiki/` at root group notes:
  - `wiki/YouTube transcripts/` - Video transcripts/summaries.
  - `wiki/Meeting transcripts/` - Sync calls and project requirements.
  - `wiki/Decisions/` - Key architectural and strategy choices.
  - `wiki/Projects/` - Feature maps and specs.
  - `wiki/Trading Rules/` - Setup logic, risk policies, and broker specs.
- **Auto-Memory**: `wiki/memory.md` tracks agent summaries across sessions.
- **Frontmatter**: Markdown files contain YAML metadata mapping `topic`, `tags`, `url`, and `date`.
- **Wikilinks**: Standard double-bracket `[[Page Title]]` references are parsed to calculate backlinks dynamically (backlinks populate under "See Also" sections).
- **Disk Sync**: The edge container mounts the host `./wiki` folder so Obsidian edits are synchronized live.

