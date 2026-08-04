# QuantG — AI Agent Operating Manual

**Read this first. Every session. No exceptions.**

This is the canonical reference for all AI agents working on the QuantG algo-trading platform. It supersedes all other documentation.

---

## 1. What This App Is

QuantG is an NSE/BSE options algo-trading platform.
- **Backend**: FastAPI + Motor (MongoDB) + asyncio loops. Single file: `backend/server.py` (~15k lines). Additional routes in `backend/routes/`.
- **Frontend**: React (CRA) + Tailwind. Modular architecture: pages in `frontend/src/pages/`, presenter components in `frontend/src/components/`, centralized state in `frontend/src/contexts/`. No SSR.
- **Infra**: Docker Compose on VPS. Five containers: `quantg-backend`, `quantg-frontend`, `quantg-mongo`, `quantg-caddy`, `quantg-hermes`.
- **Broker**: Upstox (V3 WebSocket feed + REST orders). No Zerodha, no MCX, no Kite.
- **Domain**: quantgtrade.com → VPS 82.180.145.183

### Current Mode
Paper trading (PAPER). Live trading infra exists but `CORE_ENGINE_LIVE_ENABLED=false` in docker-compose.yml.

### Current Operational State (updated 2026-07-05)
- **The data wall is BROKEN and the current book has NO PROVEN EDGE.** As of 2026-07-04 we can backtest option strategies on 2 years of real NSE prices (see §13). The OOS validator's verdict: **0 of 11 option strategies show an out-of-sample edge** — every real-sample strategy is `NO_EDGE_NEGATIVE` (e.g. NIFTY Theta Credit Spread −₹873/trade over 116 trades, −₹83.6k in 2025 OOS). A 72-config sweep found **0 configs** that cross positive OOS. This corroborates the live book's ~−₹86/trade. The "winning theta cluster" was small-sample illusion. **Do not tune these strategies further — that is the treadmill.**
- **NEW OPERATING DISCIPLINE (this supersedes daily strategy-tweaking):** hypothesis → **OOS backtest** (§13) → forward-paper → live. Nothing is "working" until it has 30+ trades AND survives out-of-sample. Grade *ideas* on OOS expectancy, not daily paper P&L (which is noise at ~13 trades/day). New strategies must PASS the OOS validator before deploy.
- **Tomorrow paper-forward book locked to QG-O1 + QG-O5 only (2026-07-06):** QG-O1 is the OOS-backed 3% OTM / width-10 held-to-expiry put spread. QG-O5 was founder-directed into a small intraday bull-put credit-spread scalp (ORB trigger, sell PE 2 strikes OTM, buy 1 strike lower, 60-min hold) despite n=22 being below the IMD n>=30 gate. **All other QG-O strategies and old book rows must remain archived**; startup/scheduler restore must not auto-wake archived rows. `CORE_ENGINE_LIVE_ENABLED=false` remains unchanged.
- **Planned cleanup (not yet executed):** archive + de-template the 11 dead option strategies + 10 equity strategies (kept in code templates; a DB delete alone re-seeds — see removal touch-points in §13). Pair removal with validated NEW strategies so the book is never left empty. Do off-hours as one commit + backend rebuild; the catalog tests will need updating.
- **Equity is LIVE on real data (paper)** — 10 NSE_EQ strategies. Their phantom/mock bugs are FIXED (do NOT re-apply old "equity is phantom" cautions). BUT they cannot yet be backtested (index-only bhavcopy data; stock EOD data not fetched) and their live record is loss. Candidates for removal/rework, not tuning.
- **Data-gathering phase tuning** (caps, spread `required_capital`) remains as config, but its purpose (collect trade data) is now largely served — the historical bhavcopy backtest replaces slow live data-gathering for edge discovery.
- Feed/quotes/spreads/equity run on the real Upstox V3 + REST path — see Common Pitfalls (§6).

### Session fixes (2026-07-06) — live-book infra hardening
- **Spread pyramiding STOPPED** (`63e8e53`): hold-to-theta spread strategies re-opened a fresh spread every runner cycle (8+ stacked/underlying). `signal_manager.py` now blocks a new spread entry when an active spread already exists for `(user, strategy, underlying, mode)` — the symbol-based BUY dedup missed shifting strikes and SELL-entered credit spreads bypassed it. Verified: 1 spread/strategy holds.
- **Spread P&L marks fixed** (`b3ba091`): spreads have no top-level `instrument_key`; their `legs[]` were REST-priced every tick → 429 storm → `last_ltp` blank → dashboard showed PNL ₹0.00. `_subscribe_open_position_tokens_on_startup` now also subscribes `legs[].instrument_key` to the WS feed → marks price off the warm cache.
- **Manual exit + EXIT ALL no longer create phantom shorts** (`79dce36`+`8efebda`): `/positions/{sym}/exit` and `/ops/squareoff-all` placed generic opposite orders under a `manual_recovery` bucket; the ledger nets fills by `(strategy_id, target_symbol)`, so a mismatched strategy_id created a phantom SHORT (and equity skipped on a 0 price). Both now route through `_close_strategy_positions` (the canonical monitor/EOD path — equity, single-leg, spreads, orphans). **Rule: never close a position with a generic opposite order under `manual_recovery`; always `_close_strategy_positions(user_id, pos.strategy_id, reason)`.**
- **QG-O strategy status:** only QG-O1 and QG-O5 are allowed to be paper-forward live/queued. QG-O2/O3/O4/O6/O7/O8/O9/O10 are archived and should not be restored by startup, scheduler, enable-all, or template sync. QG-O5 is no longer a debit-spread buyer; it is an under-sampled intraday credit-spread scalp and must stay paper-only until more IMD evidence exists.

### Session changes (2026-07-08) — QG-O11 + live spread execution path
- **Paper-forward book is now QG-O1 + QG-O4 + QG-O11** (`b3487db`..): QG-O5 archived (n=22, no OOS support). **QG-O11 NIFTY Regime Seller Credit Scalp** is the new OOS-validated intraday seller (three gates: bull vwap-pullback → bull-put, bear failed-bounce → bear-call, choppy RSI-fade; 1-OTM width-1, TP 35% of credit, SL 1.5×, entries 09:45–12:30 IST, ≤3/day, 1-min candles, 1 lot via required_capital 3000). OOS on 204 real 1-min days at production geometry: +₹194/trade OOS, PF 5.08, 100% green months. CAVEAT: avg credit ~₹18 — edge dies above ~5–8% slippage; forward-paper first, never scale on backtest.
- **Per-strategy credit-spread exit geometry**: `visual_config.options.credit_tp_frac` / `credit_sl_mult` flow signal_manager → `open_credit_spread(tp_frac=, sl_mult=)` → `compute_exit_levels`. Global env defaults (`CREDIT_SPREAD_TP_FRAC`/`SL_MULT`) unchanged for the theta book. GOTCHAS: any `structure=credit_spread` template gets `CREDIT_SPREAD_THETA_RISK` blanket-applied (needs a name carve-out for scalps) AND non-`OPTION_ALPHA_REBUILD_NAMES` credit spreads get `required_capital` forced to 8000 (QG-O11 added to that set); creation-time `visual_config.options.required_capital` is NOT re-synced by template sync.
- **Live (real-money) spread execution path built** (`core/live_spread_executor.py`): `open_credit_spread`/`close_credit_spread` are now mode-aware — mode="live" fills legs via real Upstox MARKET orders (entry: BUY long wing first, SELL short second, short-leg failure unwinds the long — never naked short; exit: BUY back short first, SELL long, per-leg progress persisted in `live_close_state` so partial closes resume without re-buying the short; incidents → `db.live_spread_incidents`). The position doc is repriced off REAL fills; accounting (trades/trade_fills/today_pnl) stays in spread_lifecycle, wallet ops are paper-only. **Gated fail-closed**: requires `CORE_ENGINE_LIVE_ENABLED=true` AND new `LIVE_SPREADS_ENABLED=true` (both default false) AND `live_arm_state.armed` AND real pipe instrument keys; executor wired in server startup (`_place_upstox_order` + gateway `get_order_details`). Debit spreads remain paper-only. Tests: `tests/test_live_spread_executor.py`.

### Session changes (2026-07-09) — drop hold-to-expiry, enable intraday TP/SL + re-entry
- **Order ledger shows the real option contract + F&O segment** (`7858aa9`): `normalize_order_row` (execution_bridge.py) now prefers `target_symbol` over the bare underlying and derives segment from the `instrument_key` prefix (`NSE_FO|`/`BSE_FO|`) or `structure=credit/debit_spread`; option/spread order docs carry only `symbol=underlying` + `instrument_key`, so they previously rendered as `NIFTY / NSE_EQ` (equity). Display-only; routing unchanged.
- **QG-O1 + QG-O4 no longer hold to weekly expiry** (`8d46076`, founder-directed): both now book intraday at `credit_tp_frac=0.5` (50% of credit) / `credit_sl_mult=2.0` (2× credit stop), `options.exit_mode=""` (not `"expiry"`), `risk.exit_mode="signal_or_tp_sl_trailing"`, `max_trades_day=6`. Once a spread hits target it closes and the anti-pyramiding guard releases → same-day re-entry (paper already floors the per-strategy cap to 24/day via `PAPER_MEASUREMENT_MAX_TRADES_DAY`; the real blocker was hold-to-expiry, NOT the daily cap). Removed the QG-O1 special-case block (server.py ~6674) that re-forced `hold_to_expiry`. Verified live: the open QG-O4 SENSEX spread (past its 13.05 TP) closed on the next monitor cycle for **+₹1,322.98** (`exit_reason=spread-tp`). Updated `test_seeded_strategy_exit_mode_matrix` to allow intraday credit spreads. **CAVEAT:** this removes QG-O1's held-to-expiry OOS validation (the only strategy that passed §15.5) — QG-O1/O4 are now UNVALIDATED intraday variants, aligned with the RES §15 dynamic-seller mandate but needing fresh forward-paper/OOS evidence. `CORE_ENGINE_LIVE_ENABLED=false` unchanged.
- **Correction to §6:** `max_trades_day` IS enforced (`signal_manager.py:481`) — the older "red herring" note is wrong; in paper it is floored to 24. To hold a spread to expiry again, set `options.exit_mode="expiry"` (the position monitor keys off it) — do not without founder direction.
- **RES2 gate realized-vol was a data artifact** (`290609e`): QG-O1's `res2_gate` blocked all 34 daily signals on `IV 14.2 − RV 32.6 = -18.4` — but **RV 32.6 was fake**. The bhavcopy store has a 6-month HOLE (2025-12-31 → 2026-07-01); `_recent_daily_closes` concatenated across it so `realized_vol_pct` saw a spurious ~−8% "daily" return that blew 20-day RV to 32.6% (real NIFTY RV ~6–13%). Fix (`core/entry_gate.py`): keep only the CONTIGUOUS recent run of daily closes (restart on any >6 calendar-day gap, `RES2_RV_MAX_GAP_DAYS`). Verified: RV now 6.1%, vol-edge +8.1 RICH; gate `allow=True` on RANGE, correctly blocks TREND_UP. QG-O1 was PERMANENTLY starved by this; now it trades on RANGE+rich-premium days. The store's Jan–Jun 2026 daily gap was BACKFILLED same day (`docker exec -u root quantg-backend python /app/scripts/bhavcopy_ingest.py 2026-01-01 2026-06-30 --source nse` — 108 days/604k rows; **must run `-u root`** or the host-cron root-owned `./data` gives a container-user PermissionError). Store now full+contiguous for 2026; RV(20) on a real full window = 11.34%. OPEN OPS: the freshness cron had a Jan–Jun 2026 hole (resumed 2026-07-01) — monitor it doesn't recur.
- **Overprotection gates stripped from the seller book** (founder-directed audit): kept ALL safety guards (daily-loss kill switch, margin/risk sizing, greeks cap, anti-pyramiding, duplicate-signal, phantom-candle, exit circuit-breaker, live-preflight), removed the gates that block good trades a strategy already decided on — all env-reversible: (1) **REGIME_GATE exempt for credit/debit spreads** (`REGIME_GATE_SKIP_SPREADS=true`, strategy_runner) — the external regime veto was built for single-leg directional buyers; a spread owns its regime logic (QG-O11 brain / QG-O1 RES2 / QG-O4 range filter) with wing-capped loss; (2) **loss-streak HARD BLOCK off** (`LOSS_STREAK_BLOCK_AT=0`, signal_manager) — a streak on a validated edge is variance; soft size-throttle at streak≥4/5 still trims; (3) **trade_frequency loss-streak pause+half-cap off** (`LOSS_STREAK_TRIGGER 3→99`, `LOSS_STREAK_HALF_CAP false`); (4) **option-quality gate exempt for spreads** (`QUALITY_GATE_SKIP_SPREADS=true`). NOT touched (correctly inert/safety): REGIME_STRICT_GATE + OVEREXT_GATE (opt-in via per-signal flags, not set by live strategies), COUNTERTREND_GATE (equity-only), EXPOSURE_CAP (real correlation guard, 3/underlying, not binding). PRE-EXISTING (unrelated) red test: `test_trade_frequency.py::…boosted_cap` — "NIFTY Momentum Buyer" now classifies as `default`(cap 8) not `momentum`(16), so expected "10/10" reads "10/6"; fix the classifier or the test.
- **Time-based spread recycle** (`27933d9`, founder-directed): the spread exit engine had SL/TP/trailing-lock but **no time exit**, so a spread that drifts sideways sat until the 15:25 square-off, tying up the one-spread-per-strategy slot all day (QG-O11 took one morning scalp then idled). `position_monitor._process_spread_position` now closes a spread (reason `spread-time-exit`) after `visual_config.risk.time_exit_minutes` when no price trigger fired; price exits still take priority. Set post-`CREDIT_SPREAD_THETA_RISK` (which zeroes time_exit) in the per-strategy blocks: **QG-O11 45m** (scalp turnover), **QG-O1/O4 120m** (2h recycle). New cached helper `_strategy_time_exit_minutes`. The live book now exits on TP (50%/35% of credit) / SL (2x/1.5x) / trailing-lock / time-recycle — turnover-focused, not hold-and-hope. (Aside: "cleared manually via Ops console" sets a position to `CANCELLED` with realized_pnl 0 leaving unrealized unbooked — a separate manual-path wart, not the monitor.)
- **Midday entry cutoffs removed** (`884e0bd`, founder-directed): there are **THREE** independent afternoon gates on the live spreads — all must be widened together: (1) book-wide `ENTRY_CUTOFF_IST` default `1230`→`off` (`strategy_runner.py`); (2) per-strategy code windows `13:00`→`15:00` (QG-O1/O2/O3/O4) and `12:30`→`15:00` (QG-O11); (3) **`CREDIT_ENTRY_WINDOW`** (credit-spread-specific, `strategy_runner.py:_credit_entry_window_bounds`, applied at ~1383) `0945-1300`→`0945-1500` — **overridden by `docker-compose.yml` env** `CREDIT_ENTRY_WINDOW: "${CREDIT_ENTRY_WINDOW:-0945-1500}"`, so the code default alone is NOT enough; the compose env wins. Reason string `ENTRY_WINDOW: credit_spread entry at HH:MM outside …`. Verified: QG-O4/O11 now enter in the afternoon. (Equity has its own `EQUITY_ENTRY_CUTOFF=1430`, unrelated.) — the 12:30/13:00 blocks were starving the live book of the whole afternoon. 09:45 open-guard kept; per-position 15:25/15:10 square-off still prevents holding into close. **QG-O11** (scalper) was taking one morning trade then sitting because 12:30 blocked afternoon re-entry; it can now take its 3 daily scalps across the session (NOTE: afternoon window DEVIATES from its OOS geometry validated on 09:45–12:30; forward-paper judges). **IMPORTANT nuance:** this does NOT make QG-O1 trade — QG-O1 is independently blocked by its **RES-2 gate** (`res2_gate:true`): it only sells when IV−RV is rich AND regime=RANGE; on a TREND_UP day with realized>implied vol (e.g. IV 14.2 < RV 32.6) every signal is `RES2_GATE_BLOCKED` by design. Time-cutoff removal helps QG-O4/O11 (no vol gate), not QG-O1.
- **Spread CLOSE now writes exit `orders` rows** (`8f06b9b`): `close_credit_spread`/`close_debit_spread` recorded the exit in `trade_fills`+`trades`+the position doc but **never inserted `db.orders` rows for the exit legs** — so the order ledger showed spread ENTRIES only, never the closing trades (the entry legs were the only rows). Added `_record_spread_exit_orders` (spread_lifecycle.py): one FILLED CLOSE order per leg (BUY-to-close short, SELL-to-close long) at the real exit LTPs, net P&L on the short-close row; display-only (canonical P&L stays in trade_fills); best-effort. Backfilled 2026-07-09's already-closed SENSEX spread. **Pitfall:** any new spread-close path must write both the trade_fills P&L row AND the two audit order rows.

---

## 2. Critical File Map

| What you want to change | File |
|---|---|
| API route logic (orders, signals, strategies) | `backend/server.py` |
| Ops routes (enable-all, emergency-stop, reconciliation) | `backend/routes/ops.py` |
| Auth routes | `backend/routes/auth.py` |
| AI bot routes | `backend/routes/ai.py` |
| Pre-trade risk checks (Greeks, kill-switch, sizing) | `backend/core/risk_manager.py` |
| **EdgeMath sizing core** (pure Kelly/vol-target/day-governor, §16) | `backend/core/edge_sizer.py` |
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
| Frontend AI bot components | `frontend/src/components/aibot/` (ChatFeed, AgentContextPanel, PromptSuggestionsPanel) |
| Frontend app shell / layout | `frontend/src/components/Layout.jsx`, `frontend/src/components/ui/` |
| Frontend CSS design system | `frontend/src/index.css` (HSL variables, Sora/Inter/Mono fonts, blink keyframe animations) |
| Docker build/env config | `docker-compose.yml`, `backend/.env` |
| Reverse proxy config | `Caddyfile` |
| Hermes sidecar engine | `hermes/agent.py` (watchdog alert loop, pre-market readiness, EOD report) |
| Hermes deployment runbook | `docs/DEPLOY_HERMES.md` |
| **EOD options-history ingest (bhavcopy)** | `backend/scripts/bhavcopy_ingest.py` (NSE + BSE UDiFF → gz store) |
| **Bhavcopy store reader** (OHLC + option chains) | `backend/core/bhavcopy_store.py` |
| **EOD options backtester + walk-forward OOS** | `backend/core/eod_options_backtest.py` |
| **OOS validation scorecard** (per strategy) | `backend/scripts/run_oos_validation.py` |
| **Edge-search sweep** (config grid → OOS) | `backend/scripts/run_edge_sweep.py` |

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

> **Known issue — mongo healthcheck flap (2026-06-22):** `quantg-mongo`'s healthcheck (a mongosh ping with a 5s timeout) intermittently reports *unhealthy* even though mongo is fully serving (mongosh sometimes takes >5s to start). Because backend `depends_on` mongo `condition: service_healthy`, `docker-compose up -d backend` can fail with `dependency failed to start: container quantg-mongo is unhealthy`. **Workaround (mongo is genuinely up — verify backend was already connected/healthy):** `docker-compose up -d --no-deps backend`. **Real fix (do OUTSIDE market hours, recreates mongo):** loosen the mongo healthcheck `timeout`/`start_period` in docker-compose.yml. Never `down -v`.

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
| Mark-to-market `update_one` on `strategy_positions` with no status filter | Always filter the write on `status: {"$in": [...]}` matching what you queried — guardian (5s) and monitor (30s) run concurrently and can close a position mid-await; an unguarded `$set` will restamp stale ltp/pnl onto an already-CLOSED doc (fixed 2026-06-21 in `position_monitor.py`/`position_guardian.py`) |
| Looking up Upstox quote response by the pipe instrument key | Upstox **REST** `/market-quote/*` returns its `data` dict keyed by `EXCHANGE:SYMBOL` (**colon**, e.g. `NSE_INDEX:Nifty 50`), NOT the pipe key you sent. The **WS V3** feed uses pipe keys. Match both/colon/suffix or you silently get None → fallback (caused the "Simulated feed" bug, fixed 2026-06-22 `822f062`) |
| Running single-leg staleness/LTP logic on spreads in `position_guardian` | The guardian must **skip** `structure in (credit_spread, debit_spread)` — spreads have no top-level `instrument_key` but DO carry a top-level `option_type`, which trips the single-leg staleness guard → entry-price fallback → force-close at 300s at a loss. `position_monitor._process_spread_position` owns spreads (prices both legs via REST). Fixed 2026-06-22 `635add2` |
| Spreads are NOT 1-lot capped | Single-leg trades obey the "1 contract" max-lot cap; **spreads bypass it** and size by `required_capital` via `core/spread_builder.lots_for_risk` (lots = budget ÷ per-lot-max-loss). To change spread size edit the strategy's `required_capital`, not the lot cap |
| Changing the per-strategy DAILY_CAP via `max_trades_day` | The live DAILY_CAP gate is in `trade_frequency.py` `_CLASS_CAPS` (class-based: scalper/momentum/trend/swing/default, env-overridable `FREQ_CAP_*`). The `max_trades_day` field does NOT drive it (red herring). Spread/unclassified strategies = "default" class |
| Equity intraday candles only fetched via V2 `/historical-candle/intraday` | That endpoint returns **today only** → <20 bars early in the session → silent `mock-5minute` fallback (entries on fake prices). Equity uses **V3 multi-day historical + today's V3 intraday merged** (`get_historical_candles`, clamp lookback ≤25 days — Upstox rejects minute-history >~1 month, `UDAPI1148`). Fixed 2026-06-22 `372751b`/`7e57536` |
| `parse_iso_dt` used in monitor/guardian without importing it | Import from `core.position_lifecycle` (NameError crashed the staleness path, fixed 2026-06-22 `e406d10`) |
| Closing a position by placing a generic opposite order under `manual_recovery` (old `/positions/{sym}/exit`, `/ops/squareoff-all`) | The ledger nets fills by `(strategy_id, target_symbol)`; a mismatched strategy_id creates a **phantom SHORT** (and equity skips on a 0 price). **Always `_close_strategy_positions(user_id, pos.strategy_id, reason)`** — the canonical monitor/EOD path (equity, single-leg, spreads, orphans). Fixed 2026-07-06 `79dce36`/`8efebda` |
| Hold-to-theta spread strategy re-opening every runner cycle (pyramiding) | The symbol-based BUY dedup misses shifting strikes and SELL-entered credit spreads bypass it. `signal_manager.py` blocks a new spread entry when an active spread exists for `(user, strategy, underlying, mode)`. Fixed 2026-07-06 `63e8e53` |
| Spread positions showing PNL ₹0.00 (marks never update) | Spreads have no top-level `instrument_key`; their `legs[]` must be WS-subscribed or the monitor REST-prices them every tick → 429 → blank `last_ltp`. `_subscribe_open_position_tokens_on_startup` subscribes `legs[].instrument_key`. Fixed 2026-07-06 `b3ba091` |
| Intraday 1-min strategies (QG-O5..O10) never firing | The runner's `_price_history` (server.py ~16994) hardcodes `interval="5minute"`; strategies built for 1-min never trigger. Per-strategy `candle_interval` (default 5min); clamp days≤2 for 1min (Upstox UDAPI1148 rejects long minute-history) |

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
| `agent_tool_audit` | Audit logs of all read-only tool executions run by Hermes |
| `daily_reports` | End-of-day strategy performance aggregates (compiled at 15:35 IST) |
| `ai_chats` | Chat session records, carrying the tools_used citation metadata envelopes |
| `trade_attribution` | HSI Stage 1: one row per CLOSED trade with the "why" dimensions (bias/regime/structure/hold/exit_reason/R_multiple). Written at EOD by `core/trade_attribution.py` |
| `hermes_observations` | HSI Stage 2: structured, sample-size-honest EOD observations distilled from attribution rollups (claim/dimension/metric/value/sample_size/confidence) — Stage 3 scores these |
| `hermes_lessons` | HSI Stage 3: scored lessons keyed by (dimension,bucket) with a candidate→active→decayed lifecycle. Re-tested each EOD against fresh attribution (confirm/contradict → hit_rate + confidence); deterministic, no LLM. Written by `core/hermes_lessons.py`; read via the `get_hermes_brain_health` tool |

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
8. **2026-06-21 correction to #3**: a code-level audit found `today_pnl` is NOT actually double-counted — `portfolio_ledger.py` (single-leg) and `spread_lifecycle.py` (spreads) write disjoint trade-type domains and are each independently idempotent (`processed_fill_ids` unique index / atomic OPEN→EXITING claim). The confirmed live race is on `strategy_positions` mark-to-market fields: `position_monitor.py` (30s) and `position_guardian.py` (5s) both await an LTP quote before writing `unrealized_pnl`/`last_ltp` with no status filter, so a concurrent close mid-await could get overwritten with stale data. Patched in both files by adding `status: {"$in": [...]}` to those `update_one` filters (4 sites) — same compare-and-swap pattern `spread_lifecycle.py` already used for its close claim. Remaining unaudited writers on `strategy_positions`: `position_reconciler.py` and the admin-route writes in `server.py`.

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

### 11.11 Stage 2 progress (2026-07-09)
Founder approved `core.portfolio_ledger` as the owner of `strategy_positions`,
canonical `trade_fills` as the source for derived strategy P&L, and
deprecate-before-delete treatment for parallel legacy paths. ARCH-2A is complete:
position monitor and guardian route live mark/freshness writes through
`PortfolioLedger.update_position_mark`, preserving the existing status-guarded
compare-and-swap and mark calculations. Lifecycle transitions remain distributed and
are the separately gated ARCH-2B rung.

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


---

## 13. Strategy Research — OOS Backtesting Discipline (added 2026-07-04)

**This section is now the governing law for strategy work. It supersedes the old "notice a red day → tweak a strategy → deploy" loop.**

### 13.1 The data wall is solved
Upstox 404s on expired-option history made option backtesting impossible for months (old blocker `WR-71`). Fixed 2026-07-04: NSE publishes a free daily F&O bhavcopy (UDiFF) with per-contract EOD OHLC, **settlement price**, underlying price, OI and volume for every index derivative. `backend/scripts/bhavcopy_ingest.py` downloads + filters it to a gzipped per-day store at `/opt/QuantG/data/bhavcopy_fo/<year>/`. Backfilled **2024-01-01 → 2025-12-31 (494 trading days, ~2.5M option rows)**. SENSEX/BANKEX are BSE (Akamai-gated — not fetched; the one data gap).

### 13.2 The tools
- `core/bhavcopy_store.py` — reads the store → daily underlying OHLC (from index futures) + option chains (settle prices).
- `core/eod_options_backtest.py` — prices the live structures (single_leg/credit/debit spread) **settle-to-settle, daily granularity**, modelling theta decay, expiry settlement, brokerage + 3% adverse slippage/leg. `walk_forward()` splits per-YEAR (OOS = latest) + per-MONTH → verdict: `CANDIDATE_EDGE` / `FRAGILE` / `NO_EDGE_NEGATIVE` / `INSUFFICIENT_DATA`. `run(params=)` overrides tp/sl/width/DTE for sweeps.
- `scripts/run_oos_validation.py` — per-strategy scorecard. `scripts/run_edge_sweep.py` — config grid → OOS.
- Run: `docker exec quantg-backend python /app/scripts/run_oos_validation.py`. (Modules are `docker cp`'d into the running container for ad-hoc runs; **bake them into the image on the next off-hours rebuild** so they ship normally. Data must be at `/app/data/bhavcopy_fo` inside the container.)

### 13.3 Honest limits
Daily granularity + multi-day hold (2–10 DTE) ≠ the live intraday hold; intraday VWAP degenerates on daily bars. So this validates **signal + structure held to theta**, not tick-perfect intraday execution — but it AGREES with live P&L, so the convergence is the signal. It cannot backtest scalpers (need paid 1-min data; those are the losing cluster anyway) or equity (index-only data).

### 13.4 The verdict (2026-07-04) and what it means
**0 of 11 option strategies have an OOS edge. 0 of 72 swept configs cross positive OOS.** The credit-spread exit geometry (risk 100% of credit to make 50%) is structurally negative — it needs ~67% WR and runs 33–48%. **Do not tune the existing book.** Archive the dead strategies (§ removal touch-points below) and design NEW hypotheses that PASS the validator.

### 13.5 The discipline (mandatory for any new strategy)
```
hypothesis → OOS backtest (run_oos_validation / run_edge_sweep) → forward-paper (3–6 wks) → live pilot
```
- Nothing is "working" until it has **30+ trades AND positive out-of-sample**.
- Grade IDEAS on OOS expectancy, not daily paper P&L (noise at ~13 trades/day).
- Before building, run the **base-rate studies** first (short-vol vs long-vol, straddle-to-expiry, condor-in-range, underlying trend) so new strategies come FROM what the data shows, not from intuition.
- Exception note: the `QG-O1`..`QG-O10` Options Alpha Rebuild pack was seeded active in paper on 2026-07-05 by explicit founder direction so it can be watched in the next live market session. Treat results as evidence collection only until the promotion ladder is satisfied.

### 13.6 Removing a dead strategy (touch-points — a DB delete alone re-seeds)
Strategies are re-created from CODE templates in `server.py` on startup. To remove one: (1) its dict in `DEFAULT_OPTION_STRATEGIES` (:~3394) or `STANDARD_STRATEGY_CATALOG` (:~5950); (2) `UPGRADED_DEFAULT_STRATEGY_CODE_BY_NAME` (:~5991); (3) `STRATEGY_DISPLAY_NAME_RENAMES`; (4) `CREDIT_SPREAD_THETA_NAMES` (:~1536); (5) `EQUITY_CAPITAL_TIERS` (:~1562); (6) `_debit_names`/`_credit_names` migrations (:~16914/16949); (7) DB: set `status="archived"` (preferred over delete — keeps P&L history); (8) update the catalog tests. Do it off-hours as one commit + rebuild, paired with validated replacements.

---

## 14. Intraday 1-Minute Options Pipeline (IMD) — the second OOS judge (added 2026-07-06)

The EOD bhavcopy OOS engine (§13) judges **held-to-theta** structures on daily settle prices. It **cannot** judge intraday option BUYERS (`QG-O5`..`QG-O10` — ORB/VWAP/tail-event debit buyers) which live and die on minute-level moves. The IMD pipeline is the separate, legal, minute-granular judge for those. **All code (IMD-01..IMD-10) shipped 2026-07-06; it is judge-first and returns `INSUFFICIENT_DATA` until real minute data exists.**

### 14.1 Legal data rule (unchanged)
No pirated/scraped/mystery datasets. Only broker/API data under the account's terms (Upstox expired-instruments `/v2` 1-minute candles — proven usable in IMD-00), official exchange feeds, or clearly-licensed open data. Nothing else enters `data/`, Mongo, Edge Lab, or a promotion report.

### 14.2 The modules (all `backend/`)
| Layer | File | What it owns |
|---|---|---|
| Schema | `core/options_minute_schema.py` | canonical 16-field candle, IST normalize, per-row + manifest checksum, DQ flags |
| Resolver | `core/expired_option_resolver.py` | `(underlying,date,expiry,strike,type)` → Upstox `expired_instrument_key` or typed reason; NIFTY/BANKNIFTY only (SENSEX/BANKEX blocked) |
| Store | `core/options_minute_store.py` | gzipped-CSV per contract-day under `data/options_1m/…`, write + reader (`get_option_minutes`/`get_chain_at_time`/`missing_minutes`/`coverage`) |
| Importer | `scripts/options_1m_ingest_upstox.py` | bounded fetch (NIFTY/BANKNIFTY, ATM±N, CE+PE), idempotent, `--dry-run` |
| Capture | `core/options_minute_capture.py` + `core/live_option_capture.py` | forward live tick→1-min bar aggregator + store flush. **Feed WIRED 2026-07-10** (`74fd75d`): `live_option_capture.LiveOptionCapture` is a read-only V3 tick listener (attached next to the index capture at server startup ~17748); refs registered from open-position/spread legs (NIFTY/BANKNIFTY only) at startup + once/min during market hours for intraday-opened spreads (also WS-subscribes new leg keys); EOD flush 15:35 IST next to the index flush. Only registered contracts captured; unknown ticks ignored; listener errors swallowed by the feed wrapper (never touches trading) |
| Selector | `core/intraday_option_selector.py` | no-lookahead single_leg/debit_spread pick from a chain snapshot |
| Backtester | `core/intraday_options_backtest.py` | deterministic minute event loop, exits STOP→TARGET→TRAILING→TIME/SQUAREOFF, fail-closed on missing price |
| OOS | `core/intraday_options_oos.py` + `scripts/run_intraday_options_validation.py` | walk-forward verdict + `GATE`; persists `db.intraday_options_oos_runs` |
| UI/API | `routes/ops.py` (`GET/POST /ops/intraday-oos`) + `frontend/src/pages/Analytics.jsx` (`IntradayOOS` panel) | shows verdicts + coverage, labelled distinct from the EOD theta OOS |

Store format is **gzipped CSV** (matches `bhavcopy_store`), not Parquet — pyarrow/duckdb aren't installed and adding them is a founder-gated rebuild.

### 14.3 Data layer (both sources exist + proven on real data, 2026-07-06)
- **Options 1-min**: `scripts/options_1m_ingest_upstox.py` → `data/options_1m` (proven: real Jan-2025 NIFTY fetch, idempotent, 0 errors).
- **Underlying index 1-min**: TWO sources — historical import `scripts/index_1m_ingest_upstox.py` (Upstox v3 active minutes) → `data/index_1m`, AND live forward capture (IMD-04 WIRED: `core/live_index_capture.py` attached to the feed via `add_tick_listener`, flushed 15:35 IST by `_daily_scheduler_loop`, `core/index_minute_store.py`).
- `run_intraday_options_validation` builds real store-backed providers from both and produces real intraday trades (verified QG-O5/O6 = 5 trades on 5 days). **Remaining is SCALE only** — backfill ~3 months so the sample crosses the `GATE` (30 trades / 3 months); a Jan–Mar 2025 NIFTY backfill was started 2026-07-06. Run either importer in-container: `docker exec -u root quantg-backend python /app/scripts/<importer>.py --from … --to … --underlyings NIFTY` (writes to the `./data` bind mount).

### 14.4 Intraday promotion ladder (the law for QG-O5..QG-O10)
```
hypothesis → IMD 1-min OOS (run_intraday_options_validation) → forward-paper (3–6 wks) → founder-gated live pilot
```
- The `GATE` (`core/intraday_options_oos.GATE`): **≥30 trades, ≥3 months, ≤20% missing-minute rate, OOS expectancy > 0 after costs, ≥50% green months.** A thin sample or poor coverage → `INSUFFICIENT_DATA`/`DATA_QUALITY_FAIL`, never a flattering row.
- Paper P&L alone NEVER proves an edge — only a passing OOS verdict + forward-paper does.
- Live promotion stays **founder-gated**; `CORE_ENGINE_LIVE_ENABLED=false` by default. No UI control seeds or tunes a strategy.

### 14.5 Daily forward-capture health checklist (IMD-04 index + option capture now wired)
- subscribed option contracts > 0 and matches the tradeable set,
- `bars_written` climbing during 09:15–15:30 IST,
- `stale_feed_seconds` low (feed alive),
- EOD `flush_day` `data_quality_gaps` reviewed — a `MISSING_n_MINUTES` gap is recorded, never back-filled with fabricated bars.

---

## 15. Real-Edge System Roadmap (RES) — founder-directed rebuild (added 2026-07-08)

**This is the current active build program. It supersedes ad-hoc strategy work.** The founder rejected the existing book on 2026-07-08 after a −₹4,571 day: the book holds ONE position all day, has no intraday profit-lock (green round-tripped to red), and put two of three positions on the *same* NIFTY bull-put bet — QG-O1 even sold puts into a `TREND_DOWN` and lost ₹5.2k. The mandate: replace the "sell one spread and sit" machine with a **dynamic, regime-aware seller scalper** that banks profits, trails, re-enters, rotates CE/PE with the market, cuts losers fast — and is **validated on the OOS judge before it trades**.

**Framing law:** there is no "foolproof" system. The target is **validated + cost-robust + risk-controlled**, not guaranteed. More intelligence ≠ a smarter LLM or more indicators — it is truthful costs, regime/vol conditioning, and portfolio risk. **Hermes stays the researcher/disciplinarian, NEVER the trader** (LLM narrates, code computes).

### 15.1 The 8 tasks (build in dependency order)
```
✅ 1. Realistic cost model                       ← DONE 2026-07-08 (see §15.2)
✅ 2. Market Intelligence Engine (5 signals)     ← ENGINE DONE (core/market_context.py); wiring lands w/ RES-7/8
✅ 3. Dynamic exit engine (bank + trail + fast stop) ← DONE (core/dynamic_exit.py, wired into position_monitor)
✅ 4. Re-entry / multi-trade policy              ← DONE (core/reentry.py); wiring w/ RES-7
✅ 5. Side-rotation (CE vs PE from context)      ← DONE (core/side_selector.py); wiring w/ RES-7
✅ 6. Portfolio risk layer                       ← DONE (core/portfolio_risk.py); wiring w/ RES-7
✅ 7. Seller scalper BRAIN                        ← DONE (core/seller_scalper.py decide()); unactivated until founder
✅ 8. OOS gate                                   ← DONE (core/res8_oos.py). VERDICT: buyer dead (5th time); RES-2 gate turns the 3% OTM put spread from NO_EDGE→CANDIDATE_EDGE (both years +ve). See §15.5
```
### 15.5 RES-8 OOS verdict (2026-07-08, NIFTY 2024–25 real bhavcopy)
- **BUYER confirmed dead (5th time):** IV-cheap+trending debit spread = NO_EDGE_NEGATIVE, −₹380/tr, n=50, both years negative. Stop building option buyers.
- **The RES-2 market_context gate demonstrably ADDS edge:** on the EOD-gradeable 3% OTM put spread held-to-expiry, ungated = NO_EDGE_NEGATIVE (−₹14/tr, 2025 −₹145); gated by IV−RV rich + RANGE it's monotonic — CANDIDATE_EDGE at min_edge 0.0 (n=36, +₹112/tr, both years +ve, 74% green), rising to +₹487/tr at the strictest gate (n shrinks <30). Monotonic expectancy-vs-gate = a real signal, not a fluke. **First strategy in the rebuild to PASS OOS.**
- **CAVEATS:** the intraday scalp geometry (ATM width-1) held DAILY is negative — the EOD judge is the wrong instrument for the intraday scalp exit; that needs the IMD 1-min judge (§14). Bull-biased (2 up years). `CORE_ENGINE_LIVE_ENABLED=false` — founder-gated.

### 15.6 Intraday scalp verdict (2026-07-08 — IMD judge on 204 real 1-min days)
- Data was ALREADY backfilled: `data/options_1m` = 204 days (2024-08-23..2025-06-25) + `data/index_1m` 498 days. No fetch needed.
- **Made the core IMD judge seller-capable** (was buyer-only): `intraday_option_selector` credit_spread branch + `intraday_options_backtest.run_day` is_credit path (book `credit_tp_frac`, stop `credit_sl_mult`×credit, trail peak, sign-correct slippage). 3 tests. This lets the CORE judge grade sellers, not just the scratch harness.
- **VERDICT (`scratch/regime_seller_oos.py`, local-only, gitignored):** regime-gated intraday seller ALL_w1 = **CANDIDATE_EDGE, n=587, 88% WR, +₹232/tr, PF 3.71, net +₹136k; OOS n=54 +₹139/tr, 91% green; all 3 regimes +ve.** Both judges (EOD gated put spread + IMD scalp) now confirm the seller edge.
- **CRITICAL — slippage-fragile:** cost stress passes at 2% slip, FRAGILE at 5% (OOS −₹57), DEAD at 10% (−₹218). avg credit only ₹21. → forward-paper (RES-1 slippage now in paper) is the true test; never scale on backtest.
```
Items 1–6 are reusable machinery; 7 is the strategy; 8 is the truth check. `CORE_ENGINE_LIVE_ENABLED=false` stays until founder-gated after #8.

### 15.2 Task 1 — realistic cost model (SHIPPED, commit 06b04c2)
Paper credit spreads filled BOTH legs at raw MID premium with zero bid/ask crossing, on entry AND exit → paper P&L overstated every spread edge (worst for a frequent scalper). Fix in `core/spread_lifecycle.py`: `_apply_paper_slippage(price, side)` + `PAPER_SPREAD_SLIPPAGE_PCT` (env, default 0.03), applied per leg, **paper-only** (live fills are real, untouched): open `else` branch (SELL short below mid, BUY long above mid → recompute net_credit/max_loss); close `if not is_live` branch (BUY back short above mid, SELL long below mid → widens close_value). Mirrors what the OOS engines already do (eod 3%/leg, intraday 2%/side). NOTE: single-leg paper already had slippage via `execution_router._estimate_slippage`; only the spread path (the whole live book) was free. Consequence: **the existing book's paper P&L now reads worse — that is the illusion being removed, not a regression.**

### 15.3 Task 2 — the Market Intelligence Engine (Phase 1 spec)
Produce ONE `market_context` snapshot bundling five signals, **computable identically live (feed) AND historically (index_1m + option-minute stores)** — the non-negotiable rule, because a signal that can't be reconstructed historically can never be OOS-validated. ~60% of the base already exists (`market_regime.py`, `iv_regime.py`, `order_flow.py`).

| # | Input | Role | Status | Build |
|---|---|---|---|---|
| A | **IV − realized_vol** | THE edge — sell only when implied vol is expensive vs delivered vol, and the gap is fat | 🆕 NEW (`iv_regime` has IV level only) | compute realized vol from index returns; emit signed, sized richness; sell-gate fires only when positive & above threshold |
| B | **Regime (trend/range/high-vol)** | Gate + side-picker — seller edge lives in RANGE, dies in TREND | 🔧 FIX `market_regime` (price-only, thresholds fixed, flags NOT enforced) | add vol-state, vol-adjust thresholds, ENFORCE `short_entries_allowed` |
| C | **Chain intel: OI walls, PCR, skew** | Which side is safer to sell (feeds #5) | 🆕 mostly NEW | read chain snapshot → richer/safer side |
| D | **Top-of-book order-flow imbalance** | ENTRY FILTER only (no latency edge on ₹5k VPS) — avoid selling into a sweep | 🔌 WIRE (`order_flow.py` exists) | feed in "full" mode so bid/ask sizes populate; veto only, never trigger |
| E | **Event calendar** | Fat-tail gate — don't sell premium into expiry-day/macro/results | 🆕 NEW | known-events table; flagged day → block/shrink seller entries |

Plus **F** (the `market_context` bundle object, one interface both scalper and backtester read) and **G** (historical reconstruction of A–E). Recommended Phase-1 build order: **A → B → F → G → C → E → D**.

### 15.4 Where the state lives
Harness task tracker + `TASKS.md` (RES-1..RES-8 block) + memory `project_realedge_roadmap_costmodel_07_08.md`. Keep all four in sync when a task lands.

---

## 16. EdgeMath (EM) — continuous edge-based sizing & P&L intelligence (added 2026-07-09)

**Active program. Founder mandate: no more hard gates/blockers — replace every binary allow/block with a continuous edge→size function.** The book should size UP when a strategy's own edge is fat and the day is green, and fade toward ZERO as the edge decays or the day turns red — smoothly, never a block. **Honest target: `E[daily P&L] > 0` with an asymmetric, bounded loss tail** (small capped losses, larger let-run wins). NOT a guaranteed green day — variance forbids that; say so plainly, no "this concept should work" hearsay.

### 16.1 The three layers (all PURE — one code path for live monitor + OOS backtester)
- **L1 signal→edge score:** expectancy `E = W·μ_win − (1−W)·μ_loss` + payoff `b = μ_win/μ_loss`, from the strategy's OWN rolling closed trades (source: `trade_attribution.attribution_rollup`, already computes win_rate/expectancy by regime/bias/hold_bucket).
- **L2 edge→base size:** fractional-Kelly conviction (`f = W − (1−W)/b` → conviction∈[0,1] vs `EDGE_KELLY_REF`) × volatility-target lots (`EDGE_RISK_PER_TRADE_PCT × equity ÷ per-lot-max-loss`). Weak/negative edge → conviction→0 = **soft stand-down** — this is the loss-streak GATE replaced by math.
- **L3 day-P&L governor:** `m = clamp(1 + day_pnl/budget, 0.25, 1.5)` — green compounds, red de-risks (each further trade smaller → no revenge blow-up) + profit ratchet (gave back > `EDGE_DAY_GOV_GIVEBACK` of peak → clamp to min). **This is the "loss ≤ profit" shaping** — asymmetry, not a promise.

### 16.2 State + conflict map (verified 2026-07-09)
Plan/tasks in **`TASKS.md`** (EM-1..EM-9, top of ACTIVE QUEUE) + memory `project_edgemath_sizing_system_07_09.md`. Keep both + the harness tracker in sync.
- **EM-1 DONE:** `core/edge_sizer.py` (pure, 25 tests, isolated — nothing imports it yet; ships on the next backend rebuild at EM-4).
- Sizing injection point = `signal_manager.py:845/890` (`_spread_lots = lots_for_risk(...)` → `open_credit_spread`). EM-4 replaces this, capped to `risk_manager` capital + `core/capital_model` margin.
- **Biggest overlap: `core/profit_lock.py`** is an existing L3 that HARD-BLOCKS re-entry (`day_profit_locked`, its only writer; `signal_manager` reads+blocks). **EM-5 must convert it to continuous size-down (no block).**
- Legacy `alloc_mult` loss-streak throttle (`signal_manager.py:1110`) → subsumed by EM-4. Spreads bypass the single-leg 1-lot cap → EM targets the spread path first.

### 16.3 Discipline
`OOS proof (EM-7) BEFORE live wiring (EM-4)` — dynamic sizing must beat FLAT sizing on OOS expectancy AND compress the daily-loss distribution before it touches the live path. `CORE_ENGINE_LIVE_ENABLED=false` throughout. Hermes narrates the "why"; the code computes the numbers.

### 16.4 Dynamic contract edge (landed 2026-07-09)
`core/dynamic_contract_selector.py` rescans the current chain on every credit-spread entry instead of treating a fixed offset as the strategy. It ranks CE and PE candidates across several deltas using continuous credit/width, theta, OI-liquidity, delta-fit, directional-fit, time-remaining, and repeated-signature factors. Reusing the same expiry/strikes is penalized, not blocked. The winner carries `contract_edge_score`, `contract_size_mult`, `selection_signature`, and factor telemetry into `spread_lifecycle`; lots and TP/SL geometry fade or expand continuously from that contract evidence. This is an EM L1 contract-quality input. It does not override the EM-7 law: portfolio sizing from rolling expectancy remains unapproved until dynamic sizing beats flat sizing OOS.

### 16.5 EdgeMath complete in paper-forward mode (2026-07-09)
EM-2..9 are wired. `signal_manager._edge_math_spread_size` combines cached strategy/regime rolling expectancy, fractional-Kelly conviction, wallet-equity volatility-target size, contract quality, the day-P&L governor, profit-ratchet multiplier, and the defined-risk capital cap. `profit_lock` banks gains then sets `day_profit_size_mult=0.25` instead of blocking re-entry. EOD writes observe-only `edge_math_advice`; `/execution/snapshot` and Analytics expose the decision. The founder explicitly waived EM-7 as a promotion blocker because the daily judge cannot reproduce intraday chain rotation. The harness remains available, but clean forward-paper telemetry is the active judge. This waiver does not enable real trading: `CORE_ENGINE_LIVE_ENABLED=false`.

## 17. Edge Lab Research Ledger (ERL, completed 2026-07-09)
`core/edge_research_ledger.py` upgrades Edge Lab snapshots into a reproducible research ledger. Each strategy/config/data-window/cost combination receives a deterministic hash in `strategy_trials`; reruns increment `run_count` instead of flooding Mongo. Snapshot rows carry robustness, multiple-testing penalty, parameter-plateau score, explicit reject reasons and a promotion stage. `core/historical_regimes.py` adds no-lookahead trend/vol/gap/large-move tags and per-regime expectancy. Evidence allocation is paper-only (`auto_apply=false`). Analytics Edge Lab v2 displays the promotion ladder, parameter heatmaps and recommended paper capital. Routes: `GET /api/ops/edge-lab/trials` plus the existing snapshot/refresh routes. Hermes and ERL share the existing HIRB evidence/math philosophy; ERL does not trade or mutate strategies.

---

## 18. Regime-Aware Ensemble (RAE) — the whole-system plan (added 2026-07-10)

**This is the umbrella program the platform is now building toward. It does NOT discard RES (§15) or EM (§16) — it consumes them.** Founder-directed after the 2026-07-10 trend-up day: every strategy in the book is a premium seller, so on the ~1% of days that trend, the whole book bleeds and the best case is "everyone wisely does nothing." A book whose best case on a trend day is inaction is a single strategy with extra steps.

### 18.1 The finding that forces the design (real 498-day NIFTY 1-min study, `scratch/regime_directional_oos.py`)
The market is a **regime machine**, and each regime has a different winner:

| Regime | % of days | Winner | Covered today? |
|---|---|---|---|
| RANGE (choppy, small net move) | **60%** | premium sellers | ✅ the whole book |
| INSIDE_QUIET (tight range) | **26%** | sellers **+ VWAP mean-revert (+₹164/day, 51% WR — unexploited)** | ⚠️ half |
| HIGH_VOL_CHOP (big range, no net direction) | **13%** | *nothing clean wins* → stand down | ❌ we trade & bleed |
| TREND_UP/DOWN (like 2026-07-10) | **~1%** | delta-1 directional (+₹1,383–3,132/day on trend days) | ❌ uncovered |

Two hard truths from the study: (1) a delta-1 trend module (deep-ITM/future, ~zero theta — the fix for why every OTM-option buyer died 5×) **IS profitable on trend days** (+₹3,132/day, 80% WR with the strict gate); (2) but a loose intraday gate fires on ~90 days/yr and is **net-negative** because ~130 RANGE days fake a breakout and revert — **the entire edge is in gate PRECISION, not in the strategy.** That is the RES-2 lesson generalized: QG-O1's "IV-rich + RANGE" gate works because it is slow and reliable; "this is a trend day" is fast and easily faked, so the trend module needs an equally-precise gate (the IV-**cheap** mirror of QG-O1 + higher-timeframe alignment).

### 18.2 The system = 7 blocks (what all we must build; ✅ = already exists)
1. **Data & Truth** — index 1-min (498d)✅, option 1-min (204d + live capture wired 2026-07-10)✅, bhavcopy EOD✅, realistic cost model (RES-1)✅, IV/VIX✅. Gap: keep option-minute capture accumulating; freshness watchdog.
2. **Regime Classifier — THE new organ.** No-lookahead, live+historical parity, outputs `(label, confidence)` over the §18.1 taxonomy. Substrate exists: `market_context.py` (RES-2, ~5 signals) + `core/historical_regimes.py` (ERL, no-lookahead trend/vol/gap tags). Missing: the discrete regime output + confidence + wiring as a router input.
3. **Specialist Library — one per regime, each stands down outside its regime** (RES-2 discipline everywhere): RANGE/INSIDE → sellers (have) + INSIDE mean-revert (NEW); TREND → delta-1 directional (NEW, needs the precise gate); HIGH_VOL_CHOP → **stand-down as a first-class strategy** (NEW); EVENT → stand down / defined-risk only (NEW).
4. **Router / Capital Allocator — NEW, but EdgeMath is the substrate.** Regime+confidence → activate the owning specialist(s) → size via EdgeMath (§16). Stand-down is a legal output (chop → ~0 size). EM L1–L3 exist✅; new part is *gating activation by regime* instead of running every strategy every day.
5. **Dynamic Exits & Portfolio Risk — mostly built** (RES-3 dynamic exit✅, RES-6 portfolio risk✅, profit-lock✅, exposure caps✅); NEW: exits keyed to regime (trend trails wide, range books fast).
6. **The Judge, reformed — regime-conditional OOS.** Current OOS grades a **blended all-days** number that averages a specialist's good regime with its bad one — the reason it looks like it "kills good strategies." Reform: grade each specialist **only on its regime's days**, walk-forward within regime, and treat small-sample as *needs-forward-paper*, **not** an auto-veto. This is the fix for the founder's (justified) OOS distrust on small samples.
7. **Hermes + Telemetry — built**✅; extend to show the live "regime of the day + who is active + who is standing down" + per-regime P&L. Narrates only; never trades.

### 18.3 The plan (dependency-ordered; each phase ships independently) — tracked as RAE-0..RAE-7 in TASKS.md
`0 taxonomy locked → 1 classifier → 2 REFORM THE JUDGE (before any specialist, so it can be graded honestly) → 3 specialists one at a time (3a chop stand-down = pure risk cut, ship first; 3b inside mean-revert = easy win; 3c trend delta-1 + IV-cheap gate = the hard one; 3d sellers = RANGE specialist) → 4 router (EdgeMath activation by regime) → 5 regime-aware exits → 6 forward-paper the ensemble + Hermes telemetry → 7 founder-gated live pilot.` `CORE_ENGINE_LIVE_ENABLED=false` until Phase 7 founder gate.

### 18.4 The governing law (so this does not become the old tune-on-noise treadmill)
- **Every specialist owns ONE regime and MUST stand down outside it.** Coverage, not omnipotence — no strategy is expected to win every day; the *ensemble* covers the day-types.
- **Judge on the regime, not on all days** (the OOS-distrust fix). Blended numbers lie about specialists.
- **Stand-down is a strategy.** On HIGH_VOL_CHOP, not-trading is the edge.
- **Precision > payoff for the trend gate.** Trend winners are huge; the failure mode is range fakeouts, so the gate must be selective (IV-cheap + daily-trend alignment), validated by whether it flips the *all-days* number positive.
- **No specialist scales on backtest** — regime-conditional OOS → forward-paper-on-regime → founder-gated live. Hermes narrates the "why"; code computes the numbers.
- **What already exists is the foundation, not the gap:** EdgeMath sizing, dynamic exits, portfolio risk, the data layer, and one proven regime-gated strategy (QG-O1). The gap is the classifier, the router, the non-seller specialists, and the regime-aware judge.

### 18.5 Build status (2026-07-10) — RAE-0..6 built, RAE-7 gated
All pure modules, all validated on the 498-day NIFTY index store; live hooks are OBSERVE-ONLY behind `RAE_ROUTER_ENABLED` (default false → zero trade-path change until the founder flips it in paper).
- **RAE-0** `core/regime_taxonomy.py` — canonical labels + thresholds + `classify_day` + `REGIME_OWNER` + base rates (match the study to the decimal).
- **RAE-1** `core/regime_classifier.py` — no-lookahead `classify_intraday`→`RegimeSnapshot(label,confidence)`, maturity-aware. Validated: intraday TREND call is 16% precise (trends rare/fakeouts common) but confidence separates (0.95 correct vs 0.83 fakeout).
- **RAE-2** `core/regime_conditional_oos.py` — `evaluate_regime_conditional`: grades ONLY on-regime days, walk-forward within regime, thin→`NEEDS_FORWARD_PAPER` never a veto (the OOS-distrust fix).
- **RAE-3** `core/regime_specialists.py` — `chop_stand_down` (3a; avoids ~₹72k on 65 chop days), `inside_mean_revert` (3b; +₹103/day INSIDE, NEEDS_FORWARD_PAPER), `trend_delta1/long/short` (3c; +₹423/day trend, NEEDS_FORWARD_PAPER + needs an IV-cheap gate), sellers=RANGE owner (3d, REGIME_OWNER).
- **RAE-4** `core/regime_router.py` — `route()`: regime ownership + CHOP/EVENT stand-down + trend confidence gate → `size_mult` (0 = stand down). Wired observe-only into `signal_manager._edge_math_spread_size` (`telemetry["router"]`, skip `RAE_ROUTER_STAND_DOWN` only when enforced). Validated: naive book −₹203k → routed −₹5.6k (+₹197k, stands down 250/498 days).
- **RAE-5** `core/regime_exit.py` — `regime_exit_params()` tunes the RES-3 trail (TREND wide / RANGE tight); wired into `position_monitor` as a no-op unless enforced. Hard SL/TP untouched.
- **RAE-6** `GET /api/ops/regime-status` — ensemble watch view (regime per index, per-strategy ACTIVE/STAND_DOWN, P&L by entry-regime). Forward-paper STARTS when the founder sets `RAE_ROUTER_ENABLED=true` in paper.
- **RAE-7** `GET /api/ops/rae-live-readiness` — read-only pilot gate (READY/NOT_READY + blockers). NEVER enables anything; live is the founder flipping `RAE_ROUTER_ENABLED`→paper-evidence→`CORE_ENGINE_LIVE_ENABLED`+`LIVE_SPREADS_ENABLED`+arm.

**To activate the ensemble (founder):** set `RAE_ROUTER_ENABLED=true` (paper) → watch `GET /api/ops/regime-status` accumulate regime-bucketed P&L → when `GET /api/ops/rae-live-readiness` reads READY, decide on the live flags. `CORE_ENGINE_LIVE_ENABLED=false` until then.

---

## 19. Hermes Diagnostician — daily deterministic system auditor (added 2026-07-17)

Founder-directed after the −₹6,821 day: the bugs that caused it (side inversion, no-op stop, unpriced-leg exit skip) were **deterministic facts sitting in the data** — nobody was checking the right invariants daily. The Diagnostician (HSI Stage 6) is the fix: it **finds problems across trading-logic, strategy-edge, infra and data every day, files them with evidence, and NEVER fixes** (finds-and-files, like all of Hermes; agents/founder fix).

**Governing law (same as HSI): code computes every finding, the LLM only narrates.** A `Finding` is a deterministic probe output carrying raw `evidence` + a `reproduction` query. The narrator (`narrator.py`, Gemini, fail-open to a deterministic briefing) may explain/rank CONFIRMED findings but can **never invent one** — a suspected new issue must become a *probe*, never a raw claim. Anti-hallucination spine: an LLM can be confidently wrong; a probe counting `exits where reason in (TP,SL)` cannot.

**Location:** `backend/core/hermes_diagnostics/` — `contract.py` (Finding/Severity/Confidence/Domain), `probe_sdk.py` (`@register`, ProbeContext preload, safe-exec so a probe crash becomes a finding not an abort), `runner.py` (`run_diagnostics(db,user,date,kinds=)` → build ctx → run probes → verify → dedup+persist → auto-resolve), `narrator.py`, `probes_{static,execution,infra,strategy}.py`. **5 layers:** Probes → Runner → Verifier (thin evidence = SILENCE, never a guess) → Narrator → Loop (a finding whose probe ran but didn't re-emit auto-resolves = verify-on-fix).

**12 probes / 4 domains** (permanent regression guards for §-above RCs): `exec.intent_vs_execution_side` (RC-1), `exec.exit_reason_mix` (RC-2/3: 0 price-exits across ≥4 spreads = dead stop engine), `exec.no_op_stop` (sl_value≥width), `exec.spread_mark_staleness` (RC-3 intraday), `exec.specialist_regime_fit` (off owned_regimes); `static.reward_risk_geometry` (breakeven WR ≥0.75), `static.specialist_tag_consistency`, `static.spread_capital_sanity`; `infra.feed_regime_artifact` (|intraday_return|>20% bad tick — catches the −58% NIFTY CRASH), `infra.overgated_book`; `strategy.persistent_live_loss`, `strategy.thin_sample_grading` (<30-trade honesty).

**Persistence + surface:** `db.hermes_findings` (one doc per `key`=probe::entity; dedup bumps occurrences; auto status=resolved when a fix stops the probe emitting), `db.hermes_diagnostic_runs` (per-run summary + narrative). Wired into the EOD pipeline (`position_monitor.py`, after lessons/advisor, best-effort). Read: `GET /api/ops/hermes-diagnostics?status=open|resolved|all`; manual run: `POST /api/ops/hermes-diagnostics/run?date=`. Handoff is **review-first** (founder reads findings, then triggers fixes — no auto-task-filing yet). Adding a permanent probe for each newly-confirmed bug is the standing practice: a bug caught once is caught forever.

---

## 20. Edge Rebuild Program (ERP) — the active master plan (added 2026-07-19)

**TASKS.md was REWRITTEN 2026-07-19 into this program's phases** (the old 295KB queue is preserved in git history; completed programs are one-liners in its ARCHIVE). Source: Edge Reports v1–v3 (memory `project_edge_report_v3_full_07_19.md`), produced from a full app + DB census + live web verification.

**The census that forced it:** lifetime book = **−₹68,011 over 494 closed trades**; NOT ONE strategy has both n≥30 and positive P&L (every winner is n<20 noise, every n≥30 row negative; equity sleeve −₹26,157/152; QG-O2/O5..O10 never traded). Diagnosis: one bet (short intraday index premium) expressed ~11 ways, in India's most algo-contested arena (SEBI FY25: individuals −₹1.06L cr net losses, 91% losers; FPI/prop profits 96–97% algorithmic; Jane Street ₹36.5k cr 2023–25 and trading again), at credits below the cost floor (width-1 scalps spend ~100% of expected edge on friction), validated on a 2024–25 bull-only tape with founder overrides of the few validated geometries.

**Phases (task detail in TASKS.md):** **P0** purge + Strategy-Registry migration (delete ~32 dead rows + code templates; KEEP 9 paused: QG-O1, QG-O4, QG-O11, 3 RAE sellers, 3 RAE trend) + cost-floor & contract-spec-drift Diagnostician probes. **P1** data: accept stock derivatives in bhavcopy ingest (the downloaded files already contain them; the IDO/IDF filter discards them), earnings-dates store, 2019–2023 backfill, participant-wise F&O OI. **P2** analytics: `core/iv_surface.py` (per-strike richness z-scores), Deflated Sharpe + trials-count in ERL, event-conditional judge mode. **P3** sleeves: S1 earnings IV-crush premium on stock options (flagship, ~700 events/yr), S1b PEAD, S3 daily delta-1 momentum + overnight drift, S4 restored slow premium core (QG-O1 held+gated — the only OOS pass ever), S5 participant-OI overlay. **P4** Hermes v2 research analyst: corpus+RAG, opportunity probes, falsifiable hypothesis cards into ERL, calibration scoreboard, Gemini 3 Flash (free-tier) upgrade.

**New laws (added to the §13/§14 discipline):** (1) **cost-floor** — reject at design time any structure whose expected edge < 3× modeled round-trip friction (Carver's ⅓-of-Sharpe speed limit); (2) **breadth** — prefer independent bets (events × names × regimes) over more parameterizations of one bet (Grinold-Kahn IR = IC·√BR); (3) **overfitting** — verdicts carry trials-count + Deflated Sharpe; (4) **verify-live-facts** — never assert or "fix" exchange microfacts from model memory; verify via the Upstox instrument master / live sources. Verified 2026-07: NIFTY lot **65**, BANKNIFTY **30**, SENSEX **20** (Jan-2026 revision, NSE circular FAOP70616); NIFTY weekly expiry **Tuesday**, SENSEX **Thursday**, BANKNIFTY monthly-only.

**Open founder gates:** D-1 purge-list approval, D-2 Upstox Plus (expired-instruments backfill), D-3 paid research-lane model. `CORE_ENGINE_LIVE_ENABLED=false` throughout; live remains the RAE-7 founder ladder.

### 20.1 Phase 0 execution (2026-07-19)
Codex executed the ERP Phase 0 cutover after founder approval. A pre-purge VPS snapshot exists at `/opt/QuantG/data/archive/book_snapshot_2026-07/` with `strategies`, `strategy_positions`, `trade_fills`, `signals`, signal counts, and manifest. The registry manifest now lives in `core/strategy_registry.py`: keepers are QG-O1, QG-O4, QG-O11, and the six RAE specialists; all are paused/paper with `registry.active=false` until re-judge/forward-paper. The approved purge list is deleted from `db.strategies` by `scripts/phase0_registry_cutover.py --apply` after upserting keeper rows into `db.strategy_registry`.

Startup no longer pushes code-template or structure migrations back into strategy docs: `_migrate_strategy_code_versions`, debit/credit spread structure rewrites, alpha-repair followup rewrites, and v12 template sync were disabled. Diagnostician now has `static.cost_floor` and `infra.contract_spec_drift` probes; `market_domains.contract_spec_for_underlying()` is the central configured contract-spec helper. `CORE_ENGINE_LIVE_ENABLED=false` remains unchanged.

### 20.2 Phase 1 data-foundation code (2026-07-19)
P1 data foundations are landed and runtime-populated for the ERP judge path. `scripts/bhavcopy_ingest.py` accepts stock F&O instruments (`STO`/`STF`), legacy pre-2024 NSE F&O files, and configurable `--instr-types`; `BhavcopyStore` reads `STF`/`IDF` futures for `underlying_daily()` and `STO`/`IDO` option rows for `option_chain()`. As of 2026-07-19 the VPS store has 2024–2026 stock/index F&O EOD coverage plus 1,234 pre-2024 index F&O trading days from 2019-01-01 through 2023-12-29; the 2020 OOS validator smoke runs successfully in the backend container. `core/earnings_calendar.py` + `scripts/earnings_calendar_ingest.py` provide a file-backed earnings calendar and `events_for(symbol,start,end)`. `scripts/earnings_calendar_fetch_nse.py` fetches official NSE board-meeting financial-results dates for the default top-30 F&O names; the backend scheduler refreshes a ±45-day forward window weekly on Saturday 05:00 IST. The VPS runtime earnings store has 309 deduped events across 193 dates from 2024-01-11 through 2026-07-18 (RELIANCE=11, TCS=10). `core/india_flows.py` has participant-wise F&O OI parsing/storage, `scripts/participant_oi_ingest.py` handles local files, and `scripts/participant_oi_fetch_nse.py` fetches official NSE all-reports archive CSVs. The VPS participant-OI store has 1,359 available weekdays / 5,436 rows from 2019-01-01 through 2024-07-05; NSE labels that report discontinued from 2024-07-08, so the fetcher caps there. `data.store_coverage` watches earnings and participant-OI freshness. Upstox Plus is founder-confirmed active as of 2026-07-19; QuantG exposes Plus capability and real store coverage through `GET /api/upstox/data-health`, Broker Keys, Market Hub, Hermes Research Lab, and Hermes read-only tool `get_upstox_data_health`. Deep options-minute coverage is verified on VPS through the Upstox expired-instruments path.

### 20.3 Phase 2 analytics foundation (2026-07-19)
P2 analytics/judge reform is landed. `core/iv_surface.py` builds a pure file-store IV surface from bhavcopy option chains: Black-Scholes bisection IV, per-strike/per-expiry points, near-expiry skew, term summary, and trailing ATM-IV richness z-score. That surface now feeds `market_context`, optional seller-gate richness telemetry/blocking (`IV_SURFACE_SELLER_MIN_Z`), dynamic contract-score factors, read-only `/ops/iv-surface`, and the Edge Lab Analytics UI snapshot. ERL rows carry explicit `trials_count`; `deflated_sharpe` is a probability computed only from normalized held-out-year trade returns and fails closed when that vector is absent. Trial identity includes tenant, strategy configuration, judge version, cost model, and return basis. The EOD judge supports stock F&O rows through the P1 store and event-conditional mode via signal filtering around supplied earnings/event dates; `scripts/run_oos_validation.py --mode event` exercises that path. `core/judge_facade.grade(strategy_cfg, mode=eod|event|intraday|regime)` and `/ops/judge/grade` provide the unified judge contract, with intraday/regime modes served by durable host-worker jobs. P2-5 added EOD research pricing for `calendar_spread`: short near expiry and long far expiry at the same strike/type, marked daily with near-expiry intrinsic on settlement and far-leg bhavcopy premium; Edge Lab proposal UI/API can now request it. This is research-only; no live calendar-spread execution path exists. `scripts/options_1m_ingest_upstox.py` also gained `--sleep-sec` because the overnight Upstox Plus run hit broker 429s when fetching too quickly. These changes are analytics/data only; live trading remains disabled.

### 20.4 Phase 3 S1 earnings IV-crush validator (2026-07-19)
P3-1 is implemented as a research-only validator, not as a live or paper-woken strategy. `core/earnings_iv_crush.py` builds the S1 flagship sleeve over the top-30 liquid stock-F&O names: defined-risk iron condor, exact T-1 entry signal, forced T+1 exit, no event-expiry entry, and no expiry-week event entry because of physical-settlement risk. `scripts/run_earnings_iv_crush_validation.py` runs the universe and prints sample count, OOS expectancy, Deflated-Sharpe proxy, 3x cost-floor multiple, verdict, and a final `eligible_for_paper` gate. The gate must stay closed unless all are true: n >= 300 trades/events, verdict is `CANDIDATE_EDGE`, DSR passes, and expected edge is at least 3x modeled round-trip friction. Local code/tests can validate the rules with a fake store; the real verdict must be run on the VPS/populated stock-F&O bhavcopy store before any registry paper wake.

### 20.5 Phase 3 S3/S4 validators (2026-07-19)
P3-2 and P3-3 are implemented as research-only validators. `core/daily_delta1_momentum.py` re-horizons the RAE trend buyer into daily time-series momentum: 20-day close-to-close continuation plus a separate overnight-drift variant, deep-ITM single-leg pricing (`itm_offset_pct=0.02`), five-day hold, reported vol-target lots, DSR, and a multi-regime paper gate. `scripts/run_daily_delta1_validation.py` is the CLI. `core/slow_premium_core.py` restores the QG-O1 held-to-expiry 3% OTM / width-10 put-spread candidate and adds a SENSEX monthly defined-risk strangle/condor candidate; both use P2-1 IV-surface richness gates before creating sell signals. `scripts/run_slow_premium_validation.py` is the CLI. These validators do not seed/wake registry rows and do not touch broker execution. Any paper wake still requires the printed OOS/DSR/gate evidence to pass on the populated VPS store.

### 20.6 Phase 3 remaining validators and book assembly (2026-07-19)
P3-4..P3-7 are implemented in `core/phase3_remaining.py` with CLI `scripts/run_phase3_remaining_validation.py`. P3-4 is the preliminary PEAD test: follow the stock future in the event-day direction for a 5-day post-earnings window; it is intentionally killed unless sample/expectancy/DSR pass, because QuantG still lacks surprise-magnitude data. P3-5 validates participant-OI as an overlay only, using the official P1-4 store to test FII-vs-client futures bias; it returns `wire_overlay=false` unless the overlay proves edge. P3-6 re-judges paused sellers through an EOD proxy using 0.50/0.90 TP/SL geometry and 2/5/8% slippage stress for QG-O11, the RAE sellers, and QG-O4 artifact check; founder wake/delete remains outside the validator. P3-7 assembles a non-mutating book summary from all Phase 3 sleeve gates and reports heat/correlation caps. It does not seed, wake, size, or route real strategies.

### 20.7 Phase 4 model baseline (2026-07-19)
P4-1 is landed. The default Gemini chat/planner/narrator/wiki model is `gemini-3-flash-preview` (`routes/ai.py`, `core/embeddings.py`, `core/hermes_diagnostics/narrator.py`, `routes/wiki.py`, `server.py`) and `backend/.env.example` is aligned. Runtime env overrides were updated locally and on the VPS so `GEMINI_MODEL` no longer pins the old Flash model. Embeddings intentionally remain `gemini-embedding-001` with 768-dimensional vectors; this preserves RAG/Hermes memory compatibility while the chat/tool-planner model moves to Gemini 3 Flash preview.

### 20.8 Phase 4 research analyst complete (2026-07-19)
P4-2..P4-6 are landed as a read-only Hermes research analyst subsystem. `wiki/Research/` contains 26 curated corpus notes and `core/research_rag.py` now indexes those disk notes into `db.hermes_memory` as `type=research` with stable source refs. `core/phase4_research.py` plus `scripts/run_phase4_research.py` provide deterministic opportunity probes, citation-required hypothesis cards, persistence helpers for `db.research_signals` and `db.research_hypotheses`, calibration summaries, and paid-lane status. `/ops/research-signals/run` persists probe snapshots; `get_hermes_brain_health` includes `research_calibration`. The D-3 paid research-lane decision remains founder-gated: without `HERMES_PAID_RESEARCH_ENABLED=true` and an explicit research model env var, weekly synthesis stays on the free model lane.

### 20.9 Phase 4 audit hardening (2026-07-19)
Post-implementation audit fixes closed the Phase 4 truthfulness gaps. Hypothesis-card citations now validate real corpus refs and ready probe refs instead of accepting any non-empty citation, default cards cite relevant corpus/probe pairs, calibration prefers actual verdict/status evidence over stale `untested` blobs, research signals are user-scoped, RAG reindex deletes stale `type=research` rows when notes are removed, backend Docker root context excludes nested env files, and the existing daily scheduler runs the Phase 4 probe/card refresh weekly on Sunday 05:30 IST.

---

## 21. Credit-Spread Geometry Laws (added 2026-07-21)

**Two structural laws every premium-selling strategy must satisfy BEFORE any signal work.** Both were derived from a live-chain measurement (3 underlyings × 3 expiries × 6 widths × 5 deltas, taken during market hours), not from a model. Violating either makes a strategy negative-expectancy before the market moves — no signal, regime gate or sizing logic can rescue it.

### 21.1 Law 1 — Cost floor (now ENFORCED at build time)
`core/spread_builder.credit_cost_floor` vetoes any credit spread whose (a) credit/width ratio < `CREDIT_SPREAD_MIN_CREDIT_RATIO` (0.12) or (b) bankable profit (`tp_frac × credit × lot_size`) < 3× round-trip friction (`SPREAD_ROUND_TRIP_COST_PER_LOT`, **300**/lot). The veto lives in `build_credit_spread` — the single choke point every credit spread passes — and `select_dynamic_credit_spread` drops failing deltas from the ladder entirely. Research paths opt out with `enforce_cost_floor=False`; the EOD/intraday judges have their own pricing and are untouched.

Measured: `short_delta` 0.12 clears the floor in **ZERO** geometries; width-1 clears in **ZERO** cases at any delta/expiry; on a 0-DTE afternoon **none of 60** geometries clears. Delta 0.30 clears at 3.0–6.4×. Smaller lots sit closer to the floor — SENSEX (lot 20) needs a wider wing than NIFTY (lot 65).

**Pitfall that caused this:** the law existed in research validators and as Hermes finding `static.cost_floor`, but nothing stopped the LIVE path opening the trade. Its friction constant was also 3.5× too low (85 vs the real ~300/lot, the figure `dynamic_exit.TRAIL_MIN_ARM_RUPEES` already encoded), so bad geometry passed the probe.

### 21.2 Law 2 — Theta reachability (NEW)
`core/spread_builder.tp_reachability(tp_frac, dte_days, hold_minutes)`; probe `static.tp_reachability` fires below 0.55.
```
theta_reachable_frac = hold_minutes / (dte_days * 375)
ratio                = theta_reachable_frac / tp_frac
```
A seller's take-profit must be reachable by DECAY inside its own hold window. If not, the trade is a directional coin flip and the exit is decided by whichever clock fires first — paying round-trip friction every cycle.

Measured across the book: of **71 closed trades only 10 exited on a price trigger — 86% were clock-driven.** The ratio rank-ordered both the price-exit rate and the P&L (QG-O4 0.32 → +₹1,768, the only winner; QG-O1/RAE NIFTY/RAE BANKNIFTY 0.09 → **0 price exits across 24 trades**).

**Corollary — expiry cycle constrains strategy horizon.** BANKNIFTY is monthly-expiry only (nearest DTE 7–30), so reachability is 0.25 falling to 0.06. **BANKNIFTY cannot be an intraday theta seller at any width or delta**; the fix is a multi-day hold, not a wider spread. NIFTY weekly = Tuesday, SENSEX = Thursday, BANKNIFTY = monthly.

The two laws pull in **opposite directions** and must be solved together: the cost floor pushes toward nearer expiry and fatter credit; reachability pushes toward shorter DTE and longer holds; 0-DTE fails the cost floor outright.

### 21.3 The deployed book (2026-07-21)
All 8 live credit sellers: `short_delta` 0.30, `credit_sl_mult` 0.90, `time_exit_minutes` 300. NIFTY/BANKNIFTY width 4 / TP 0.45 (~3 DTE); SENSEX width 6 / TP 0.50 (~2 DTE). Breakeven WR 0.64–0.67 (was up to 0.97). Verified live: 7 of 8 build at cost multiples 3.98–4.63; RAE BANKNIFTY correctly stands down per §21.2.

### 21.4 Rules for changing seller geometry
- **Capital caps are load-bearing.** `lots_for_risk = budget // (max_loss_per_unit × lot_size)`. Narrowing a wing without lowering the cap SILENTLY SIZES UP — the old caps would have given 2/4/9 lots (NIFTY/BANKNIFTY/SENSEX). Always re-derive `required_capital` from the new per-lot max loss.
- **Code edits do NOT reach the live rows.** ERP Phase 0 disabled startup template sync (§20.1), so every geometry change needs BOTH the code template and a DB migration (`scripts/regeometry_seller_book_07_21.py` is the pattern: idempotent, dry-run by default, prints a before/after diff).
- **Check the whole live book, not the registry.** Founder-created rows (`founder_forced_live=true`, e.g. the IDX sleeves) sit outside `ERP_KEEP_STRATEGY_NAMES` and are missed by registry-scoped fixes.
- **Research configs ≠ live configs.** `core/index_alpha_sleeves.py` is correctly held-to-expiry, where a wide wing is legitimate (theta gets its full life, the whole credit is bankable). Seeding a live *intraday* row from a held-to-expiry research config reproduces exactly this bug class — it is what the 2026-07-09 QG-O1 change did.
- **Debit spreads are exempt from both laws.** They are buyers: they pay a debit rather than collecting credit, and theta works against them, so a short hold is correct rather than a defect.
- The invariant test `test_seeded_credit_sellers_clear_the_geometry_invariants` enforces all of this on the seed catalog.

### 21.5 Both laws are now ENFORCED on the live path (2026-07-22)
Stating a law and enforcing it are different things, and until this date only Law 1 was enforced (at build time); Law 2 existed as prose plus a probe that could not see violations. Both gaps produced the −₹1,967 session of 2026-07-22 (5 of 5 spreads clock-exited).

- **Law 2 now vetoes at build time** (`build_credit_spread(hold_minutes=)`, `SPREAD_MIN_TP_REACHABILITY` 0.55, `SPREAD_ENFORCE_REACHABILITY`). The hold passed in is `min(risk.time_exit_minutes, minutes_to_close)`; `options.exit_mode="expiry"` passes None and is exempt (theta gets its full life).
- **`target_dte_days` is DECORATIVE — no selection code has ever read it.** Strategies take whatever expiry the chain offers, so a row configured for 3 DTE opens a 6-DTE contract whenever that is nearest. Never reason about a strategy's DTE from its config; measure the realized expiry. The `static.tp_reachability` probe now does.
- **Consequence — the intraday seller book is expiry-cycle-bound.** With a 300-min hold at TP 0.45–0.60, NIFTY sellers build only within ~3 DTE, i.e. Mon/Tue of the Tuesday-expiry week; SENSEX Tue–Thu; BANKNIFTY never (§21.2 corollary). Most of the week the correct behaviour is stand-down. If the book looks idle, check the veto reasons before "fixing" anything.
- **A probe that under-measures is worse than no probe.** `static.cost_floor` compared GROSS credit×lot against the floor while the law (and the builder) mean BANKABLE profit `tp_frac×credit×lot` — ~1/tp_frac too permissive, so QG-O1 (1.85×) and QG-O11 (1.61×) passed a floor they fail badly while QG-O4 was flagged on a number that meant something else. This is the second instance of the same defect class (the first: friction 85 vs the real ~300, §21.1). **When a probe and an enforcement point both encode a law, they must share the arithmetic — ideally the same function.**
- **Re-cuts carry an epoch.** `geometry_changed_at` / `geometry_change_note` on the strategy row scope realized-evidence probes to the current shape. `static.cost_floor` falls silent when the post-re-cut sample is thin (§19: thin evidence → silence); `strategy.persistent_live_loss` splits the sample and reports both sides but **never** resolves or downgrades on it — a grading reset is how a losing strategy gets laundered.

**Standing caveat:** satisfying these laws makes a structure FUNDABLE and internally coherent. It does not create edge. Every re-cut shape is UNVALIDATED and owes a judge run + forward-paper per §13.5. `CORE_ENGINE_LIVE_ENABLED=false`.

---

### 21.6 Law 3 — the exit must honour the entry gate (2026-07-29)
The week 07-27..29 won **81.8% of trades (18/22) and still lost ₹1,157**: avg win ₹307,
avg loss ₹1,669, **breakeven WR 84.5%**. Nothing was wrong with the signal. Four
independent defects let one part of the system contradict another.

**The finding.** 16 of 22 trades exited on `trail-lock` averaging **₹284** — *below the
~₹300 round-trip friction of the trade itself*. The trail armed at a flat ₹300 and gave
back 40–45% of the peak, so it banked ~60% of a peak that was itself ~half the TP. Every
one of those trades had been APPROVED by the §21.1 cost floor on the basis that it could
bank `tp_frac × credit × lot` = ₹866–1,391. **The entry gate validated a promise the exit
engine never kept** — the same defect class as §22.3 (an exemption granted by one exit
path and ignored by another) and §21.5 (one law, two arithmetics).

**Law 3:** a trailing exit may not bank less than the trade cost to place.
`dynamic_exit.trailing_lock_levels` now takes contract context and floors the arm at
`DYN_EXIT_TRAIL_ARM_COST_MULT × round_trip_friction(legs, lot_size) × lots`, and clamps
`lock_level` to the same number so a giveback cannot re-cross it. Friction is the REAL
premium-proportional figure, so BANKNIFTY (≈₹626/lot, 4.2× the flat constant) is charged
what it actually costs.

**⚠️ The multiplier is 1.5, NOT the cost floor's 3.0 — do not "unify" them.** The 3× is an
**ex-ante** criterion ("is this structure worth taking at all"); applying it per-exit is a
category error. Simulated against the 22 real trades it armed on **1 of 22** — that does
not tighten the trail, it DELETES it and sends every position to TP/SL/clock, reviving the
2026-07-10 round-trip-to-red loss. At 1.5× the trail arms on **16 of 18 winners and banks
27% more** (+₹1,920 across the week). The ex-post question is narrower than the ex-ante
one. The Hermes probe `exec.exit_below_cost_floor` judges realized wins against **1×**
friction (the unambiguous "did this win cover its own cost").

**The other three fixes (all env-reversible):**
- **Contract dedup** (`CONTRACT_DEDUP_ENABLED`): the exposure cap counts POSITIONS, so on
  07-27 three strategies each sold the **identical NIFTY 24000/24200 CE exp 07-28** and
  all three stopped out for **−₹6,469 = 97% of the week's loss** on what was arithmetically
  one bet at 3× size. Names do not protect you: **QG-O1 is called "NIFTY *Put* Spread Theta
  Core" and sold a CALL spread** — the dynamic contract selector (§16.4) picks the side from
  the live chain, and every seller reads the same chain, so they converge by construction.
  Dedup on `(underlying, option_type, expiry, short strike)` across ALL strategies.
- **CHOP stand-down restored** (`RAE_CHOP_STANDDOWN=true`): 13 of 22 trades were entered on
  a HIGH_VOL_CHOP label (8 at confidence 1.0) for −₹987, including every full-width stop.
  The 07-16 easing was trading the one regime the 498-day study (§18.1) says to sit out.
- **EdgeMath stand-down made reachable** (`EDGE_STANDDOWN_ENABLED`): §16 defines
  conviction→0 as a soft stand-down, but paper passed `floor_lots=1` and the caller applied
  `max(1, …)`, so **0 lots was unreachable — EdgeMath said "stand down" on 11 of 22 trades
  and all 11 traded at full size**, 5–7× the size it asked for. Note the router's partial
  `size_mult` (0.8) is *also* a no-op at 1 lot: `round(1×0.8)=1`. Only a full stand-down
  ever changed anything.

**Standing caveat, unchanged and important:** none of this creates edge. It removes
defects that made a winning-signal book lose money on arithmetic. n=22 is far below the
§13.5 n≥30 bar and 3 trades produced 97% of the loss, so the counterfactuals above are
**estimates, not guarantees** — removing a trade changes every subsequent state. The
Law-3 finding is structural (visible on every trade, independent of sample); the regime
and correlation findings are suggestive but thin. `CORE_ENGINE_LIVE_ENABLED=false`.

### 21.7 The judges were blind for 13 days — data one directory away (2026-07-30)
`data.store_coverage` had been reporting `bhavcopy_fo`, `index_1m`, `options_1m`,
`earnings_dates` and `participant_oi` as **EMPTY** since 07-17..07-27 (up to 17
occurrences). **The probe was right every single day.** What it could not say was WHY:
102 MB of bhavcopy (2019–2023, 1,234 days) and 1,359 participant-OI files were sitting in
**`/opt/QuantG/backend/data/`**, while the container bind-mounts the **repo-root** `./data`
to `/app/data`. A script run with `cwd=backend/` resolves a relative `data/` to
`backend/data`, which nothing ever reads.

**Why nobody acted for 13 days:** "store is EMPTY" reads as a backfill chore. "The data
exists 30 cm away and the app cannot see it" reads as an emergency. Same words, wrong
urgency. New probe **`data.store_path_mismatch`** (CRITICAL) globs the sibling roots
(`/app/backend/data`, `/opt/QuantG/backend/data`, `backend/data`, `data`) whenever a store
root is empty, and reports the file counts on both sides. **Standing rule: a probe that
reports a symptom without distinguishing its causes will be triaged as the cheapest cause.**

**Second half of the lesson — restoring it would have been WORSE than leaving it empty.**
The newest bar in that store was **2023-12-29 (944 days old)**. `_recent_daily_closes`
had gap-robustness (§ the 2026-07-09 fix) but **no check on when the contiguous run ENDS**,
and the IV-surface path used `days[-1]` unconditionally — so RES2 would have computed
realized vol and premium richness off 2023 prices and believed them as current. An empty
store fails open and says so; a stale store is confidently wrong. Third instance of the
§22.7 stale-input trap (stale `regime_fine` at confidence 1.0 was the first).
`entry_gate._store_day_is_fresh` + `RES2_RV_MAX_STALENESS_DAYS` (10) now refuse both paths.

**Ops facts:** the mounted `./data` must be owned **999:999** (container uid) — root-owned
`./data` is the §22.5 PermissionError. Restore with
`mv backend/data/<store> data/<store> && chown -R 999:999 data/<store>`. Long backfills run
as a **detached `docker run`** on the VPS (never `docker exec` — a deploy kills it):
`docker run -d --network quantg_quantg-network -v /opt/QuantG/data:/app/data -e PYTHONPATH=/app --entrypoint python quantg-backend /app/scripts/bhavcopy_ingest.py <from> <to> --source nse`.
**Still missing after this fix: `index_1m` (was 498 days) and `options_1m` (was 204 days)
exist NOWHERE on the box** — they need a fresh Upstox ingest, so the IMD intraday judge
stays blind until then.

### 21.8 Law 4 — DTE is the discriminator; gate on it, not on regime (2026-07-30)
> ⚠️ **See §23.3 — the "CHOP@DTE-0 +₹391" framing below was drawn from n=7 and is superseded
> by the full P&L: CHOP is negative except a thin untradeable DTE-0 sliver, and the chop
> standdown is CORRECT. The DTE gradient itself holds; the chop conclusion does not.**

Founder ask: **more trades, but good ones** — "relax both but in the right way, not
generalized". Measured across **259 real closed credit spreads** (the whole history),
days-to-expiry at ENTRY is by far the strongest separator and it is monotone:

| DTE at entry | n | WR | avg |
|---|---|---|---|
| **0 (expiry day)** | 56 | **80%** | **+₹123** |
| 1–2 | 49 | 63% | −₹75 |
| 3–5 | 35 | 23% | −₹121 |
| 5–7 | 31 | 35% | −₹380 |
| ≥7 | 81 | 31% | −₹235 (−₹19,015 total) |

Crossed with regime it shows **the blanket CHOP stand-down (§21.6) was mis-aimed**:
CHOP@DTE-0 is **+₹391 (n=7, 100% WR)** while CHOP@DTE-3+ is **−₹1,143 (n=6, 0% WR)**;
RANGE is negative at *every* DTE (−₹123…−₹204). **Chop is not the enemy — far expiry is.**
Standing down on all of CHOP threw away its best bucket and left DTE 3+ wide open.

**Law 4** (`core/dte_policy.py`, all env-tunable): trade **every regime at DTE 0–1**;
**owned regimes only at DTE 2**; **stand down past that**. Near expiry additionally gets
three exemptions, each measured:
- **cost-floor multiple 3.0 → 1.5** (never below 1× friction). A flat 3× vetoed DTE 0
  outright because 0-DTE credit is structurally small.
- **wing narrowed to `SELLER_NEAR_EXPIRY_WIDTH_STRIKES` (2)**. Relaxing the multiple alone
  does NOT unlock DTE 0 — the credit/width **ratio** test fails independently and rightly:
  live SENSEX 2026-07-30 was `credit 33.10 on width 800 → ratio 0.041 vs 0.120 min`.
  **The fix is a narrower wing, not a weaker law.**
- **time recycle disabled.** At DTE 0–1, `spread-time-exit` was n=8, WR 13%, **−₹661**,
  while `spread-tp` was n=24, **WR 100%, +₹555**. Clocking out mid-decay converts winners
  into losers when decay is fast enough to reach the target.

**EdgeMath's stand-down is no longer blanket** — inside the near-expiry window the DTE
evidence outranks a rolling expectancy that is itself polluted by the far-DTE trades this
law now refuses (lots floor to 1 there). Past DTE 1 it still binds.
**Concurrent spreads per (strategy, underlying) 1 → 2** (`SELLER_MAX_CONCURRENT_SPREADS`):
that limit skipped 207 signals in one week, and it is only safe to raise because the §21.6
cross-strategy contract dedup now blocks the hazard it was really guarding.

**Net effect on trade count is UP, not down:** the DTE 3+ days were already being vetoed by
the geometry laws (6,009 cost-floor vetoes in one week), so standing down there formalises
what already happened, while DTE 0–1 — previously vetoed outright — becomes tradeable.
Near-expiry windows are NIFTY Mon/Tue (Tuesday weekly) and SENSEX Wed/Thu (Thursday
weekly) ≈ **4 tradeable days/week** across the book. Verified live 2026-07-30: SENSEX 0-DTE
PE 77600/77400 opened at **width 200 (down from 800), ratio 0.252**, and
`DUPLICATE_CONTRACT_ACROSS_STRATEGIES` fired once — the −₹6,469 failure blocked in real time.

#### 21.8.1 The 2-strike wing was WRONG — it forced ATM strikes (same-day correction)
Within an hour of the DTE unlock, three SENSEX 0-DTE spreads opened at credit/width
**0.241 / 0.252 / 0.258** with short strikes **AT spot** (77600 vs 77645) and all three went
straight to a loss (−₹962 unrealized). The mechanism is arithmetic, not chance:

```
SENSEX lot 20, tp_frac 0.45  ->  bankable = 0.45 x credit x 20 = 9 x credit
relaxed near-expiry floor    =  1.5 x friction = Rs450
=> credit >= 50 REQUIRED
   credit 50 on a 200-wide wing IS the at-the-money strike (ratio 0.25)
```
**Narrowing the wing left the selector no lawful choice but ATM.** Measured DTE-0 by
credit/width: 0.10–0.16 → n=20 WR **85%** +₹182; 0.16–0.22 → n=14 WR **86%** +₹210;
**≥0.22 → n=4 WR 50% −₹206.**

**A delta cap cannot fix this.** At 0 DTE the delta curve is a **cliff** (just-OTM ~0.05,
ATM ~0.5) — there is no 0.30-delta strike, so any delta-targeted selection snaps to ATM.
The ladder already caps at 0.38 and still landed ATM. Confirmed by the data: **34 of the 56
DTE-0 trades have no delta recorded** — they were built by strike OFFSET, and won at 85%.

Fix (deployed 2026-07-30): **`CREDIT_SPREAD_MAX_CREDIT_RATIO`** — the ratio law had a floor
but **no ceiling**, and a ceiling is the only delta-independent way to keep a 0-DTE seller
out of the money. Off book-wide; the DTE policy sets **0.22** at near expiry, and
`SELLER_NEAR_EXPIRY_WIDTH_STRIKES` is now **3** (same credit → ratio 0.168). The veto text
says `credit_ratio_too_high: … short strike is at/near the money` so it is not mistaken for
a generic cost-floor block. Verified live: credit 50.31 @ width 200 → **rejected**; @ width
300 → ratio 0.168 → **passes**.

**Note the legal window is narrow for SENSEX 0-DTE:** credit ≥50 (absolute floor, lot 20)
AND ratio ≤0.22 (width ≥228) ⇒ at width 300, credit must land in **[50, 66]**. A thin-credit
0-DTE SENSEX spread (credit 40 @ width 300, ratio 0.133) is correctly refused — good ratio,
too few rupees. **Lesson, third instance this week: when a law blocks the bucket you want,
check whether the surrounding arithmetic leaves any lawful geometry at all before relaxing
the law.** Also: `dynamic_contract_selector._score` still rewards credit_ratio up to 0.35 —
above the near-expiry legal max — so the veto, not the score, is what keeps it OTM.

**Sample caveats stand:** CHOP@DTE-0 is n=7 and the ≥0.22 band is n=4 (trust the gradient's
direction, not the magnitude); the 259 trades span several geometries and strategy
generations, so this is not a controlled experiment. `CORE_ENGINE_LIVE_ENABLED=false`.

**Emergent risk this created:** with concurrency 2, one strategy opened BOTH a PE and a CE
spread at the same 77600 strike — an undesigned **short straddle**. Delta roughly cancels but
gamma doubles, and the risk layer sees two independent spreads. Needs a same-side or
combined-greeks constraint; not yet built.

### 21.9 The friction constant was wrong by ~12x — it was gating everything (2026-07-30)
> ⚠️ **See §23.2 — the "credit/width 0.09–0.21 wins" claim below is superseded by the full
> P&L: the clear book-wide winner is 0.12–0.16 only; 0.16–0.22 loses book-wide (tolerable
> only at DTE-0). The friction re-measurement itself is correct and stands.**

**Measured, not assumed.** Over **4,587 real bid/ask quotes** stored on QuantG's own signals
(`signals.greeks_at_signal.bid/ask`):

| | Measured | Modelled | Overstatement |
|---|---|---|---|
| bid-ask, % of mid | **0.252%** (median) | 3.000%/leg | **11.9×** |
| half-spread actually crossed | NIFTY 0.122% · SENSEX 0.126% · BANKNIFTY 0.157% | 3.0% | ~24× |
| brokerage + taxes, round trip | **₹14.78/lot** (n=269 real `entry_charges`) | ₹300 flat | **20×** |

**One wrong constant drove three separate failures:**
1. The **cost floor** demanded ₹900 of bankable profit where the true 3× bar is ~₹78 — this
   is the mechanism behind **6,009 cost-floor vetoes in a single week**.
2. **Paper fills** were charged the same 3%/leg, so every paper P&L the book was judged on
   was understated by roughly ₹280/lot/trade.
3. **Both OOS judges** (EOD `BACKTEST_SLIPPAGE_PCT` 3%/leg, intraday `IntradayCosts` 2%/side)
   computed **every `NO_EDGE_NEGATIVE` verdict in QuantG's history at ~12× real friction.**
   §15.6 recorded that the seller edge "dies above ~5–8% slippage" — at 0.25% real, that
   entire concern evaporates. **The judges may have been killing strategies that work.**

Founder-approved settings: **slippage 0.5%/leg** (≈4× the measured half-spread — a deliberate
buffer for 0-DTE far-OTM widening and market impact at size, still 6× cheaper than the guess),
flat floor **₹25**, both judges aligned to the same cost, **`tp_frac` 0.25**. Realized friction
is now instrument-specific: **NIFTY ₹46 · SENSEX ₹26 · BANKNIFTY ₹270** per lot — the flat ₹300
simultaneously over-charged NIFTY/SENSEX 6–12× and under-charged BANKNIFTY.

**Take-profit was unreachable.** Over 92 closed spreads with peak P&L, as a fraction of max
credit: **tp 0.15 reached by 55% · 0.25 by 33% · 0.35 by 20% · 0.50 by only 12%** (median peak
**0.174**). The book was set at 0.45–0.50 — a target the market paid on ~1 trade in 8. Worse,
`spread-time-exit` (n=35 = **38% of the sample**) had a median peak of **0.3%** — those trades
never went green at all. Migrated all 8 live sellers to 0.25 via
`scripts/retune_tp_frac_07_30.py` (template sync is disabled, §20.1 — code alone never reaches
the live rows). Both halves are load-bearing: 0.25 banks *less*, so it only clears the floor
because friction fell.

**Lot size does NOT help — measured, and the answer is no.** Charges per lot by size: 1 lot
₹7.60 · 2 ₹12.79 · 9 ₹16.08 · 10 ₹15.52. **No economy of scale** (STT/exchange fees are
proportional to turnover, not per-order), so sizing up multiplies P&L *and* variance and does
nothing for friction efficiency. Size is a multiplier on expectancy — fix expectancy first.

**The strategies are NOT one bet.** Same-day P&L sign agreement across all pairs = **108/192
= 56%** (50% = independent). The long-standing "all 8 are the same bet" worry is *not*
supported; diversification is real. Each leg is individually negative — a different problem.

**ONE RATIO EXPLAINS ALMOST EVERYTHING.** Per-strategy median credit/width vs lifetime record:
QG-O4 0.089 **+₹1,768** · RAE NIFTY 0.206 **+₹1,510** · IDX SENSEX 0.162 **+₹922** ‖
QG-O1 **0.034 −₹6,922** · QG-O11 **0.255 −₹8,075** · RAE BANKNIFTY **0.246 −₹8,667**.
**Ratio 0.09–0.21 wins; below 0.09 loses (credit too thin); above 0.22 loses (short strike too
near the money).** Now confirmed in three independent slices — all-DTE, DTE-0, and
per-strategy. It is a **BAND, not a floor**, which is precisely what §21.8.1 got wrong.
Verified post-deploy: QG-O1's morning contract (credit 11.90 / width 200) now clears the
absolute floor (₹193 vs ₹137) but is still correctly refused on ratio 0.0595 — the two laws
now do genuinely different jobs.

**Standing caveat:** none of this creates edge either. It stops the system mis-measuring
itself by an order of magnitude. Whether a correctly-costed seller book is profitable is what
forward-paper and the re-run judges will say. `CORE_ENGINE_LIVE_ENABLED=false`.

### 21.10 Averaging tested & rejected; two edge fixes; the friction sweep (2026-07-30)
**Founder asked whether pyramiding winners + averaging down losers ("most traders do this")
would help. Measured on the real 94-trade book — the answer is an emphatic NO for averaging
down:**

| Book | P&L |
|---|---|
| as-is | −₹21,934 |
| **double size on every loser** | **−₹60,247** |
| **triple size on every loser** | **−₹98,561** |

**54% of losers were never even briefly green** (peak ≤ ₹50) — they go straight against the
position, and a credit spread's loss is convex (gamma), so averaging adds size into an
*accelerating* loss on exactly the trend days that already do the damage (07-17 −₹6,821, 07-10
−₹4,910). "Most traders do this" is the argument *against* it — SEBI's 91%-of-individuals-lose
cohort (§20) trades precisely this way. Pyramiding winners is also weak here: winners
median-peak at 29% of credit and a theta winner is *nearest* its peak early, so there is no
"run" to add into. **The correct axis is between trades (EdgeMath §16), never within one.**

**A — "never went green" early cut** (`dynamic_exit.no_progress_exit`, wired in
`position_monitor`): a spread that hasn't cleared 8% of its credit within 20 min (floored at
real friction) is cut with reason `spread-no-progress`. Priced exits take priority, so a
working trade is never touched. Rationale: the `spread-time-exit` pool (38% of all trades)
median-peaked at **0.3%** of credit — dead on arrival, then left to bleed to the bell.

**B — book-wide credit/width CEILING** (`CREDIT_SPREAD_MAX_CREDIT_RATIO=0.22`): the ratio law
had a book-wide floor (0.12) but the ceiling was only applied near expiry. One ratio explains
almost every strategy's lifetime record (§21.9), and the two biggest loss pools — QG-O11 (0.255,
−₹8,075) and RAE-BANKNIFTY (0.246, −₹8,667) — are both *above* 0.22. Now blocked book-wide from
the first trade; the winning band 0.09–0.21 is untouched.

**LOTS — measured answer is NO economy of scale** (charges/lot: 1→₹7.60, 10→₹15.52; STT/exchange
fees are turnover-proportional). Sizing up multiplies P&L *and* variance and nothing else — fix
expectancy before size. **Strategies are NOT one bet** (same-day P&L sign agreement 56%, vs 50%
= independent) — the old "all 8 are one bet" worry is unsupported.

**The friction sweep — the decisive test of §21.9.** Re-ran the EOD validator over 1,868 days at
slippage 3% / 1% / 0.5% / 0.2%. Verdicts are **monotonic in friction**, exactly as predicted:

| Strategy | exp @ 3% | exp @ 1% | OOS @ 3% | OOS @ 1% |
|---|---|---|---|---|
| RAE NIFTY Range Seller | −119 (NO EDGE) | **+19 (FRAGILE)** | +153 | **+277** |
| RAE BANKNIFTY | −351 (NO EDGE) | **+32 (FRAGILE)** | −347 | −132 |
| QG-O1 NIFTY Put Spread | −425 (NO EDGE) | −175 | −83 | **+128** |

At 1% (still 4× the measured 0.25%) two strategies flip to positive expectancy and two flip to
positive OOS. **Every `NO_EDGE_NEGATIVE` verdict in QuantG's history was computed at ~12× real
cost — some of them were killing strategies that clear costs at honest friction.** CAVEAT: 1%
is FRAGILE, not `CANDIDATE_EDGE` — this says "worth forward-papering at corrected cost", NOT
"proven edge". The 0.5%/0.2% blocks were still computing at writeup; expect further improvement.
`CORE_ENGINE_LIVE_ENABLED=false`.

## 22. Full-System Audit Fixes (2026-07-24)

A whole-system audit of the 2026-07-24 session (**360 signals → 1 trade**) found five
independent defects, four of them invisible to the Diagnostician. Memory:
`project_full_system_audit_07_24.md`. All seven fixes shipped together.

### 22.1 The fine regime was NEVER real — cross-index bar leak
`live_index_capture.snapshot_minutes()` applied the `wanted` underlying filter in its
`add_row` helper but **not** in the `include_open` branch, so a request for NIFTY also
returned BANKNIFTY's and SENSEX's open bars. With the completed-bar buffer empty that is
exactly **3 "bars" at 23700 / 56400 / 75900** → `classify_intraday` → `RANGE`,
**confidence 0.027**, `ret_pct 220%`. That 0.027 is the number stamped on every skip
reason that session. Cross-check: NIFTY closed **−0.45%** while
`market_regime_state.regime_fine` read **TREND_UP confidence 1.0**.
**The RAE router was never wrong — it was fed garbage.** The 2026-07-22 "immature RANGE"
loss was diagnosed as a confidence-scaling bug and fixed as one; the input was already
corrupt. **Pitfall: when a filter is applied in a helper, check every branch that appends
to the same result list.**
`flush_day` now isolates failures per underlying and ALWAYS clears the buffer — a raised
write error used to leave the buffer un-cleared, so the next session mixed two days.

### 22.2 The nightly Edge Lab job OOM-killed the backend
`Out of memory: Killed process (uvicorn) anon-rss:9885948kB` — **9.9 GB**, `RestartCount=7`,
including a **mid-session restart at 11:24 IST**. Trigger: the 20:30 IST / 15:00 UTC
nightly rebuild; restarts followed at 15:04 on both 07-23 and 07-24.
Two compounding causes:
1. `bhavcopy_store.rows_for` was `lru_cache(maxsize=4096)` sized by **entry count** on the
   assumption slices are small. **Measured: a NIFTY day is 1597 rows / 3.76 MB**, BANKNIFTY
   2.47 MB, `load_day` 90.75 MB → 8–15 GB. Now budgeted in **rows** (`BHAVCOPY_ROWS_CACHE_MB`,
   default 1024) so entry count floats with the underlying's width.
2. **`lru_cache` on a METHOD keys on `self`.** With 45 `BhavcopyStore()` construction sites
   (one on the per-signal path) every instance got its own copy of the cache and was pinned
   alive by it. The store is now **interned per root**.
Plus: `_run_edge_lab_build` calls `clear_caches()` in its `finally`, and the container has
`mem_limit: 6g` so a runaway job fails in its own cgroup instead of the host oom-killer
choosing the trading process.
**The Edge Lab was never finishing** (it writes its cache only at the end), which is why
`edge_lab_snapshots` sat at 07-21 and `strategy_trials` at 07-20. That was the "backtest
data problem" — a crash, not data.

### 22.3 Hold-to-expiry had NEVER executed — 283 closed spreads, 0 overnight holds
`position_monitor._process_spread_position` deliberately spares hold-to-expiry spreads at
15:25 (EDR-11); `_eod_square_off_all_users(spread_phase=True)` then closed them at **15:26**
with no exemption. One `expiry-settlement` exit in all history.
This is load-bearing, not cosmetic: `build_credit_spread` waives the §21.2 reachability veto
for `options.exit_mode=="expiry"` because "theta gets its full remaining life". QG-O1 is the
only such strategy and was **the only one that could build a spread** — on a promise the
system broke daily. Its §15.5 OOS pass (QuantG's only pass ever) was for a structure that had
never once run. The backstop now skips hold-to-expiry strategies until
`_spread_past_expiry` says a position has actually reached expiry.
**Rule: if one exit engine grants an exemption, every other exit path must honour it.**

### 22.4 `GET /api/upstox/data-health` 500'd on every call since it shipped
`ImportError: cannot import name 'available_days' from 'core.bhavcopy_store'`
(`routes/broker.py`) — that name only exists on `core.earnings_calendar`; bhavcopy exposes
`BhavcopyStore().trading_days()`. Broken by `50cb129` (2026-07-19), the commit §20.2 credits
with surfacing data health. **The one screen that reports store health was itself dark.**

### 22.5 The 1-minute capture was failing on filesystem permissions
`PermissionError` writing `/app/data/index_1m/...` and `options_1m` — root-owned `./data`,
container runs as uid 999 (the §1 pitfall). Masked for `index_1m` by a root cron backfilling
a day late; `options_1m` genuinely 3 days behind. `data.store_coverage` used `_STALE_DAYS=6`,
which spans a weekend plus two working days — now **3** (`HERMES_STORE_STALE_DAYS`).

### 22.6 Observability + the Diagnostician's own blind spots
- `select_dynamic_credit_spread` discarded every candidate's reason and returned a bare
  `"no valid dynamic spread candidates"`. It now aggregates per-delta vetoes and reports the
  dominant law with its numbers; `server.py` carries it to `contract_payload["spread_veto"]`
  and `signal_manager` puts it in the SKIPPED reason. **279 of 360 signals died undiagnosable.**
- `infra.overgated_book` — the probe for exactly this question — was muted by
  **`trades > 0`** (one trade silenced it) AND a 0.8 dominance bar (actual: 279/359 = 0.777).
  It now judges **conversion rate** (`HERMES_QUIET_CONVERSION`, default 2%) with dominance
  0.6, at MEDIUM.
- New probes: **`infra.process_restarts`** (reads `db.app_starts`, written at startup;
  CRITICAL for a mid-session restart), **`data.capture_flush_failed`** (reads
  `db.capture_flush_runs`, persisted by the 15:35 scheduler), **`data.store_writable`**
  (write-tests each store root, catching the permission class *before* a day is lost).
- **Standing rule: when a probe's guard clause could mute it on the day it matters most,
  that guard IS the bug.**

Regression guards: `backend/tests/test_audit_fixes_07_24.py` (10 tests) pin every one of
these, including the exact 0.027 / 220% signature and the 360-signal / 1-trade shape.

**Standing caveat (unchanged):** none of this creates edge. It removes defects that made the
book undiagnosable and the judges unrunnable. Every strategy in the book still grades
`NO_EDGE_NEGATIVE` or `INSUFFICIENT_DATA`. `CORE_ENGINE_LIVE_ENABLED=false`.

### 22.7 Second pass (2026-07-25) — adversarial re-audit found the ROOT cause + 2 regressions
The §22.1 fix was correct but stopped one level short, and two of the §22 fixes
introduced regressions. Found by re-auditing the fixes themselves.

- **THE ROOT CAUSE: the capture tick listeners are orphaned by the daily OAuth reconnect.**
  They were attached ONCE, 8 s after boot, in `_subscribe_open_position_tokens_on_startup`.
  The founder's every-morning Upstox reconnect calls `get_user_upstox_gateway(fresh=True)`,
  which builds a new `UpstoxGateway` → new `UpstoxMarketDataFeedV3` with an EMPTY
  `_tick_listeners`. Nothing re-attached. **The live 1-minute capture has therefore never
  written a file in its life** — every `index_1m`/`options_1m` file on disk is T+2 backfill
  from the ingest cron (all exactly 376 lines; a live flush would be gappy and stamped
  ~15:35). That is why `LiveIndexCapture._bars` was empty and the classifier saw only the
  3 leaked open bars. Fix: `_attach_capture_listeners(gateway)` on every ticker (re)start.
  **Law: a listener attached to an object that gets rebuilt must be re-attached where the
  object is rebuilt, not once at boot.**
- **REGRESSION (mine): closing the leak made the stale-regime problem worse.** With the
  buffer genuinely empty, `snapshot_minutes` returns 0 rows → the scheduler's
  `if len(_bars) < 3: continue` skips the write → `market_regime_state` keeps its LAST
  value forever (it only ever `$set`s). 07-24 closed on NIFTY `TREND_UP confidence 1.0`,
  so the router would have traded Monday on Friday's label **at full confidence** — worse
  than the 0.027 it replaced, which at least advertised its ignorance. `signal_manager`
  now honours a fine regime only if `regime_fine_at` is today (IST).
- **REGRESSION (mine): interning `BhavcopyStore` froze the day index.** One instance per
  process meant `self._days` was cached for the process lifetime, so a newly-ingested
  bhavcopy day was invisible until restart — on the per-signal `entry_gate` path. Now
  TTL'd (`BHAVCOPY_DAYS_TTL_SEC` 900 s), reset in `clear_caches`, and `__new__`/`__init__`
  are lock-guarded (these stores are built inside `asyncio.to_thread` workers).
- **Edge Lab was permanently wedged**, not merely slow: `status:"building"` is written to
  the DB before the task starts while the completion flag is process-memory only, so a
  build that dies with the process leaves the doc "building" forever AND the UI disables
  its own Rebuild button. Both stored snapshots had been stuck since 07-18/07-21 — **no
  completed Edge Lab snapshot has ever existed.** Now aged out to `failed` after
  `EDGE_LAB_BUILD_TIMEOUT_SEC`.
- **`/api/core/signals` 500'd on 100% of calls** since extraction (`96e2106`): no
  `{"_id": 0}` projection, and all 3,839 signal docs carry a BSON ObjectId. The one screen
  showing WHY a strategy skipped was unreachable on the day 359 of 360 signals were skips.

### 22.8 Measured feasibility envelope for an intraday NIFTY credit seller
96 real sessions, short leg picked by **Black-Scholes delta 0.30** (an OTM-% proxy
understates near-expiry credit and gives the wrong answer — it said 22% where the real
number is 72%). Build rate = clears BOTH §21 laws at a 300-min hold:

| DTE (NIFTY weekday) | width 4 / tp 0.45 | width 8 / tp 0.30 | binding constraint |
|---|---|---|---|
| 0 (Tue) | 14% | 5% | cost floor — credit collapses to ~₹9 |
| **1 (Mon)** | **72%** | 56% | — |
| 4 (Fri) | 0% | **94%** | reachability at tp 0.45 (0.44) |
| 5 (Thu) | 0% | 0% | reachability (max 0.53 even at tp 0.30) |
| 6 (Wed) | 0% | 0% | reachability (max 0.44) |

**The current book geometry is a MONDAY geometry.** Wed/Thu are infeasible at any tested
width/tp with a 300-min hold; Friday needs width 8 + tp 0.30. This — not a bug — is why
2026-07-24 (a Friday, NIFTY 4 DTE / SENSEX 6 DTE) produced 279 build vetoes. Corrections
to §22.6: on NIFTY/BANKNIFTY the **cost floor** is the more FREQUENT veto (452 vs 308) though
reachability is the BINDING one, and SENSEX that day was 6 DTE, not 4.

---

## 23. Standing Invariants & the Anti-Overclaim Discipline (2026-07-30)

**This section is the truthful, consolidated reference for the seller book's laws. It
supersedes any conflicting statement above. It was written after a full re-audit against the
real 259-trade record that caught TWO overclaims made earlier the SAME day — proof that the
biggest threat to this app is not a missing gate but a confidently-wrong claim in these docs.**

### 23.1 THE meta-rule that would have prevented every doc-driven failure this month
**An edge claim must cite (a) sample size and (b) P&L — never win rate alone.** High win rate
on a credit spread is the *default* (you keep a small credit most days) and says nothing about
expectancy; the losing shapes in this book all have 70–80% WR. Two concrete traps caught:
- WR said "trades below 0.12 ratio win 35/21" → looked like the floor blocks winners. **P&L
  said those same trades are net −₹1,486.** The floor is correct; WR lied.
- WR said "CHOP DTE-0 is 100%" → looked like chop should trade. **P&L said the DTE-0 sliver is
  n=7 (too thin) and CHOP DTE-1+ is −₹8,600.** The standdown is correct; WR lied.

**When a larger measurement narrows or contradicts an earlier claim, CORRECT the earlier claim
in place — do not leave both standing.** The §21 laws accreted contradictions precisely because
each day's finding was appended without reconciling the last. A reader must never have to guess
which of two conflicting statements is current.

### 23.2 The credit/width ratio law — CORRECTED and final
Measured by **P&L across 259 closed spreads** (not WR, not per-strategy medians):

| ratio band | n | avg P&L | total | verdict |
|---|---|---|---|---|
| < 0.09 | 45 | −₹31 | −₹1,388 | thin credit, fat tail — reject |
| 0.09–0.12 | 11 | −₹9 | −₹98 | breakeven, thin sample |
| **0.12–0.16** | **32** | **+₹57** | **+₹1,813** | **the only clear book-wide winner** |
| 0.16–0.22 | 44 | −₹152 | −₹6,680 | **negative book-wide; positive ONLY at DTE-0** |
| > 0.22 | 129 | −₹197 | **−₹25,363** | at/near-money — the majority of all losses |

**CORRECTION to §21.9/§21.10:** the winning band is **0.12–0.16 book-wide**, NOT "0.09–0.21".
0.16–0.22 loses across all DTE and is tolerable only at DTE-0 (where there's no time for the
near-money short to be run over). The deployed guards are still correct — floor 0.12, ceiling
0.22 — because the DTE policy already stands down DTE 3+, so the residual 0.16–0.22 exposure is
mostly DTE-0 where it's fine. **But do not claim 0.16–0.22 is an edge; it is a tolerance.**

### 23.3 CHOP — CORRECTED
**CORRECTION to §21.8:** "CHOP@DTE-0 +₹391, chop is not the enemy" was drawn from n=7. Full
P&L: CHOP DTE-0 ≈ breakeven-to-slightly-positive (n=7, untradeable sample), CHOP DTE-1 negative,
CHOP DTE-2+ = −₹7,681. **`RAE_CHOP_STANDDOWN=true` is CORRECT** — it forgoes a thin DTE-0 sliver
to avoid large DTE-1+ chop losses. Do not weaken it on the strength of the n=7 number.

### 23.4 Idle is usually CORRECT — the anti-firefighting rule
**A strategy showing no trades is almost always standing down by design, not broken.** Before
"fixing" an idle strategy, read its `last_filter_reason` / the SKIPPED signal's
`rejection_detail.human_reason`. The book is built so each index trades only near its OWN expiry:

| Index | Weekly expiry | Trades (near-expiry window) | Stands down |
|---|---|---|---|
| NIFTY | Tuesday | Mon–Tue (DTE 0–1) | Wed–Fri |
| SENSEX | Thursday | Wed–Thu (DTE 0–1) | Fri–Tue |
| BANKNIFTY | **monthly only** | ~expiry-day only (§21.2 reachability) | ~28 days/month |

So NIFTY idle on a Thursday and SENSEX trading is the **designed** behaviour, not a fault. Each
index covers ~2 days/week; the ensemble covers the week. This is the single most common thing
misread as "the app broke again."

### 23.5 The load-bearing gates — NONE are useless (verified 2026-07-30)
Every gate below was checked against the P&L record and blocks a net-negative shape. There is
**nothing to remove**:
- **cost floor** (ratio ≥0.12 AND bankable ≥3× real friction) — below 0.12 is −₹1,486 net.
- **ratio ceiling** (≤0.22 book-wide) — above 0.22 is −₹25,363, the largest loss pool.
- **theta reachability** (≥0.55) — clock-driven exits were 86% of the −₹ book (§21.2).
- **DTE policy** (stand down DTE 3+) — DTE 3+ is −₹35k across 147 trades.
- **no-progress cut**, **contract dedup**, **CHOP standdown**, **EdgeMath standdown**,
  **friction 0.5%/leg** — each measured, each reversible by env.

The friction constant was the one genuinely-wrong knob (12× too high, §21.9) — that was a
mis-measurement, not a useless gate. It is now corrected.

### 23.6 Structurally-limited strategies (workable ≠ always-trading)
- **RAE BANKNIFTY Range Seller**: BANKNIFTY is monthly-expiry only, so theta reachability
  vetoes it at DTE 7/15/30 (ratio 0.46/0.21/0.11). It is workable ONLY at/near monthly expiry
  (~1 day/month) and correctly self-gates otherwise. Its −₹8,667 is pre-law history. Do NOT
  "fix" it into trading more — a wider wing does not change the expiry cycle (§21.2 corollary).
- **RAE NIFTY/BANKNIFTY/SENSEX Trend Delta-1**: single-leg buyers configured at `short_delta
  0.12` (far-OTM = the lottery-ticket / dead-buyer profile that failed 5 studies). A true
  delta-1 trend rider is deep-ITM (~0.8–1.0). They fire only on rare trend days and are
  UNVALIDATED — flag for OOS before tuning; do not enable-and-hope.

### 23.7 Nothing here creates edge — the permanent caveat
Every fix this month removed a defect or a mis-measurement. The re-run judges at honest friction
(§21.10) show FRAGILE, not CANDIDATE_EDGE — "worth forward-papering", never "proven". No
strategy is promoted to real money by any of this. `CORE_ENGINE_LIVE_ENABLED=false` stands until
the RAE-7 founder ladder, on forward-paper evidence, not on a backtest or a doc claim.

---

## 25. SEBI session change + the hold-to-expiry finding (2026-08-03)

### 25.1 Exchange fact (verified from circular coverage 2026-08-02, NOT model memory)
Effective **2026-08-03**: NSE **equity derivatives close 15:30 → 15:40 IST** (open
unchanged 09:15; index futures/options, stock futures/options; trade-modification end
stays 16:15; close-price VWAP window becomes 15:10–15:40). The **cash** segment gains a
**Closing Auction Session 15:15–15:35** for F&O-eligible stocks, so **continuous cash
trading ends 15:15**. Phase 2 (revised pre-open) is slated 2026-09-07 and is NOT modelled.
**BSE was not verified to follow** — `BSE_FO` deliberately stays 15:30.

**The two directions pull opposite ways and confusing them is the whole risk:** F&O got
10 minutes LONGER (anything stopping at 15:30 silently truncates); cash got 15 minutes
SHORTER *for continuous orders* (anything squaring off at/after 15:15 is trading into an
auction QuantG cannot participate in).

`backend/session_times.py` is the single source of truth, all env-overridable as `HHMM`.
It is a backend-**ROOT** leaf module, not `core/`, because `core/__init__` imports
`core_legacy` (DB client) and `config.py` must not pull that in.

**What the old 15:30 assumption was load-bearing for** (each of these was a real bug):
`core/market_clock` + `core/market_session_service` each carried their **own copy** of the
segment-window table; three copy-pasted `(hour==15 and minute<=30)` guards in `server.py`
gated the option-capture / VIX / chain-snapshot loops; the **15:35 capture flush** and the
**15:30–15:45 strategy auto-pause** are now *inside* the session (both moved to
`POST_CLOSE`); `spread_builder.MARKET_MINUTES_PER_DAY` and
`position_lifecycle._SESSION_MINUTES` were a hardcoded **375** — that is the *divisor* in
the §21.2 reachability law, so a stale value overstates reachable decay; the option-minute
store's last bar 15:29→15:39 (375→**385** bars).

Neat consequence: the SENSEX feasible-entry bound stays **10:20** — the square-off moving
+10 min exactly offsets the reachability denominator moving 375→385.

### 25.2 The structure shootout — the exit, not the structure, is the discriminator
1,869 real bhavcopy days (2019-01-01..2026-07-30, incl. COVID), signal held CONSTANT
(same entry days/underlying/window, corrected friction), varying ONLY structure+exit:

| structure | hold | n | WR | breakeven | expectancy |
|---|---|---|---|---|---|
| credit w=4 | 5 days | 221 | 61.1% | 64.4% | −₹282 |
| credit w=10 | 5 days | 216 | 65.7% | 68.1% | −₹338 |
| debit w=10 | 5 days | 371 | 40.4% | 42.7% | −₹247 |
| single leg | 5 days | 374 | 37.2% | 41.1% | −₹339 |
| **credit w=10** | **TO EXPIRY** | 337 | 70.6% | 66.8% | **+₹661** |
| **debit w=10** | **TO EXPIRY** | 337 | 41.2% | 35.5% | **+₹908** |

**Every early-exit row loses; both held-to-expiry rows beat breakeven** — and from
opposite directions (credit on win rate, debit on payoff b=1.82). Each early exit pays
round-trip friction and truncates the distribution before the payoff exists. Corroborated
live: 86% of exits clock-driven (§21.2), and the four biggest loss pools are all forced
exits (stop-loss −₹41,294, killswitch −₹24,859, spread-sl −₹17,712, spread-time-exit
−₹17,067). **Both rows grade FRAGILE, not CANDIDATE_EDGE** (OOS −₹2,467 / −₹1,530):
"worth forward-papering", never "proven".

### 25.3 The live-book census that motivated it (631 closed trades, −₹72,234, WR 38.8%)
- **The loss is a tail, not a drift:** worst 20 trades (3.2%) = **−₹68,044 = 94%** of the
  total loss; ex-worst-30 the book is **+₹11,460**. (Day-level: 32% green, median day
  −₹1,456 — so the book is *also* not winning on the median day. Both are true; do not
  quote one without the other.)
- **By structure** — debit n=46 WR 39.1% payoff **b=1.96** breakeven 33.8% → **+₹7,975**
  (the ONLY positive structure); credit n=297 WR 47.5% b=0.58 breakeven **63.3%** →
  −₹34,856; single-leg n=288 WR 29.9% → −₹45,353. Equity stocks n=153 WR 19.6% → −₹26,221.
- ⚠️ **Do NOT read the debit row as "buy convexity".** The 1,869-day test says a dumb long-
  premium signal loses too (−₹247/trade). The live debit sample is 46 trades over ~37 days
  in one direction-friendly month. The transferable finding is the EXIT, not credit-vs-debit.

### 25.4 The gates that made hold-to-expiry impossible
Per §22.3 hold-to-expiry had **never executed — 0 overnight holds in 283 closed spreads.**
The EOD-backstop exemption landed in `38be295`.

⚠️ **Check `backend/.env` before reasoning about which gate binds.** The founder's 07-31
override lives there, NOT in docker-compose, and it already disables several gates:
`SELLER_DTE_POLICY_ENABLED=false`, `SPREAD_ENFORCE_REACHABILITY=false`,
`SPREAD_COST_FLOOR_MULT=1.0`, `CREDIT_SPREAD_MIN_CREDIT_RATIO=0.03`,
`EDGE_STANDDOWN_ENABLED=false`. So in the CURRENT runtime the only gate still binding
hold-to-expiry was the kill-switch. **A bare `docker run` does NOT load `backend/.env`, so
verifying constants that way reports code defaults, not production.** Always
`docker exec quantg-backend` to read live config.

The two below are therefore **defence-in-depth** — they matter the moment those env flags
are turned back on, which is the stated intent (the override is explicitly reversible):
- **Kill-switch** — the book-level sweep fired 5 consecutive days, closed 29 positions for
  −₹24,859, of which **−₹20,811 (84%) were debit spreads at a 0% win rate**. The seller
  sleeve's bleed was liquidating the one structure beating its breakeven. Now **sleeve-
  scoped** (`LOSS_KILLSWITCH_SLEEVE_SCOPED`): the floor and trigger are UNCHANGED, only the
  blast radius is scoped. Two narrow exemptions — a strategy whose own day P&L is not
  negative, and a **defined-risk hold-to-expiry** position whose max loss is already bounded
  by its bought wing and reserved (`LOSS_KILLSWITCH_SKIP_HOLD_TO_EXPIRY`). Spared rows are
  also excluded from the `day_loss_locked` sweep, which would otherwise freeze them anyway.
  The exemption requires **both** conditions: a naked hold-to-expiry has unbounded loss and
  is still swept.
- **DTE policy** — `DTE_STAND_DOWN_ABOVE=2` would veto a 5–15 DTE sleeve on *every* entry.
  Exempted (`DTE_POLICY_EXEMPT_HOLD_TO_EXPIRY`). **This is the scope of the measurement, not
  a loophole:** every trade in the 259-trade DTE study exited EARLY, so "DTE 3+ loses" means
  "entering far from expiry and then being force-exited on a clock loses". The shootout
  tests the other branch. An already-expired contract is still rejected.

**Standing law this generalises (third instance of the §22.3 class):** *a gate calibrated on
intraday early-exit behaviour must exempt genuine hold-to-expiry, or the strategy exists,
looks armed, and silently never trades.* When adding any new entry gate, ask which exit
regime its evidence came from.

### 25.4b Expiry selection now honours a DTE window (2026-08-03)
`server.py` chose the expiry by **positional index** (`expiry_offset`) and nothing ever read
`min_dte_days` / `max_dte_days` — the same trap §21.5 recorded for `target_dte_days`. A
sleeve configured for 5–15 DTE therefore traded whatever the chain offered: on a Monday
that is the Tuesday weekly at **1 DTE**, a completely different structure from the one its
evidence came from.

`core.dte_policy.select_expiry()` (pure) is now the chooser. **No window configured →
unchanged positional behaviour**, so the existing book is untouched; window configured →
nearest expiry inside `[min,max]`; nothing qualifies → **stand down** with a reason.
Fail-closed is deliberate: substituting a contract the strategy did not ask for produces
P&L attributed to a geometry that never ran. Verified on the live book — 12 existing rows
still resolve `expiry_offset=0 (nearest-first)`, HTE picks 2026-08-11 (8d) not 08-04 (1d).

This is the single choke point: **spreads inherit the expiry from the same resolver**
(`dynamic_contract_selector` picks strikes *within* an already-chosen chain, never the
expiry), so credit and debit spreads are both covered.

⚠️ **NEAR-MISS worth internalising.** The stand-down branch first returned
`{"ok": False, …}`. Every caller of `_resolve_option_for_strategy` tests
`if not contract:` — a truthy dict would have been forwarded straight into
`_place_order_core` as a real contract. **When a function's failure contract is
"falsy", a descriptive error dict is a live bug, not better diagnostics.** It returns
`None` and writes `last_filter_reason`; a test greps the source to keep it that way.

**Observation this surfaced (not yet acted on):** `Tail Hedge NIFTY Far-OTM Put Spread` is
hold-to-expiry with **no DTE window**, so it takes the 1-DTE weekly — one night of
protection, then expiry. Its own `risk.max_hold_days: 8` says it intends ~8 days, and crash
insurance at 1 DTE is close to worthless. A `min_dte_days`/`max_dte_days` window is probably
the right fix; founder call.

### 25.5 What is deployed
`scripts/seed_hold_to_expiry_sleeve_08_03.py` (idempotent, dry-run default) seeds **HTE
NIFTY Defined-Risk Put Spread** (~3% OTM, width 10, 5–15 DTE, `exit_mode="expiry"`, no clock
exit, no percentage stop — the bought wing IS the stop, sized off the fixed max loss) and
**restores QG-O1** to the held-to-expiry form the 2026-07-09 intraday conversion discarded.
Both paper + armed. **Registry gotcha:** hold-to-expiry geometry must OMIT
`credit_tp_frac`/`credit_sl_mult` — present-but-0 is both out-of-range and "ambiguous exit
intent". `CORE_ENGINE_LIVE_ENABLED=false` unchanged; nothing here creates edge.

**Known test debt — and the mechanism, because it will bite again.** The suite carries
`backend/.env` into the container. Modules read their constants from `os.environ` at IMPORT
time, and `test_friction_remeasure_07_30.py` calls `importlib.reload(core.spread_builder)`.
Whatever `.env` says therefore overwrites the code defaults mid-suite, and every later test
written against code defaults fails. Proven: pinning
`SPREAD_COST_FLOOR_MULT=3.0 CREDIT_SPREAD_MIN_CREDIT_RATIO=0.12 SELLER_DTE_POLICY_ENABLED=true
SPREAD_ENFORCE_REACHABILITY=true` on the run drops failures **15 → 11** and clears 4 of the
5; the 5th is `EDGE_STANDDOWN_ENABLED=false`, also from `.env`. **None are caused by the
session change** — they are the founder override becoming visible to tests. Baseline at
`f375834` is 10 failures under the same conditions.
Real fix = read these constants lazily at call time (they are documented as
"env-reversible", which import-time reads do not actually deliver), or run tests with an
explicit env pin. **Establish a baseline at the previous commit before attributing any
suite failure to your own change** — and note a baseline run needs `-e DB_NAME=quantg` or
collection aborts and reports a misleading "0 failures".

---

## 24. Phase-5 Tracks R/J/K/M — truthful instruments & breadth (2026-08-02)

Completed the remaining ERP Phase-5 tasks (TASKS.md R/J/K/M). All research-only;
`CORE_ENGINE_LIVE_ENABLED=false` unchanged. None creates edge — they make the book's
self-measurement honest and give Hermes a way to ask new questions.

- **R3** (`core/regime_router.py`): seller-size scaling now references `SELLER_SIZE_CONF_REF`
  (0.40, the RANGE confidence ceiling) not `FINE_MIN_CONF` (0.50) — a mature range earns full
  size instead of a permanent 0.8 cap. **R4**: `dte_from_expiry` moved to module scope in
  `probes_static.py`.
- **J5** (`core/eod_options_backtest.py`): Newey-West/HAC standard error (`_hac_se`, Bartlett
  kernel) on chronologically-sorted trade P&L; `walk_forward` gates `CANDIDATE_EDGE` on the HAC
  t-stat (`t_stat_hac`), the honest one for vol-clustered/overlapping-hold returns. iid `t_stat`
  still reported.
- **K6** (`routes/ai.py`): new read-only Hermes tool `query_data_store` — FIXED verbs
  (`coverage`/`daily`/`chain`) over the bhavcopy store, store-derived underlying allowlist, capped
  rows (90 bars / 40 strikes), no free-form code; audited via `agent_tool_audit`. Lets Hermes ask
  a NEW question of the data (realized vol, OHLC, chains) without a human.
- **M4** (`core/alpha_beta.py` + `scripts/run_alpha_beta.py`): regresses each strategy's daily
  returns on a short-vol benchmark + NIFTY. `REPLICABLE_SHORT_VOL_BETA` = the strategy is
  replicable premium, not alpha (tests the §20 "one bet" thesis). Persists to `db.alpha_beta_runs`.
- **M5** (`core/score_ic.py` + `scripts/run_score_ic.py`): Spearman IC of `contract_edge_score`,
  RAE regime confidence and EdgeMath conviction vs realized forward P&L. `DECORATION` = no
  predictive content. Persists to `db.score_ic_runs`.
- **M6** (`scripts/fix_costfloor_siblings_m6.py`): dry-run-default migration raising
  `credit_tp_frac` 0.50→0.60 on the three sub-cost-floor sellers; refuses `founder_forced_live`
  rows without `--include-founder-forced`. NOTE §21.9 re-measured friction ~12× lower, so confirm
  the floor is still binding before applying.
- **Surfaces**: `GET /api/ops/alpha-beta` and `GET /api/ops/score-ic` (read latest run docs);
  Analytics → Edge Lab tab shows the `AlphaBetaPanel` + `ScoreIcPanel`. Scripts run on the VPS
  (`docker exec quantg-backend python /app/scripts/<name>.py`), write the run docs, and the UI
  reads them. **R6/M3 and M4/M5 runs + M6 apply happen on the VPS** (live ledger + populated store).

### 24.1 M5 remediation — stop sizing on non-predictive scores (2026-08-02)
The M5 run (631 closed positions) found the pre-trade scores don't predict realized P&L:
`regime_confidence` **INVERTED** (IC −0.18, t −2.33 — higher confidence → worse P&L),
`contract_edge_score` and `edgemath_conviction` ~zero IC (DECORATION). Fix, env-gated
`SCORE_SIZE_NEUTRAL` (default **true**, reversible):
- `regime_router`: no confidence-MAGNITUDE size scaling (flat per-regime base). Confidence
  still gates stand-down + trend precision (categorical, sound). This deliberately walks back
  the P5-R3 magnitude scaling — M5 proved confidence magnitude is not predictive.
- `signal_manager._edge_math_spread_size`: `contract_mult` forced to 1.0; the fine EdgeMath
  conviction multiplier is dropped (size = vol-target `base_lots` × day-governor) while the
  categorical stand-down (dead expectancy → 0 lots) and the defined-risk capital cap are KEPT.
- New Hermes probe `strategy.score_not_predictive` reads `db.score_ic_runs` and flags
  DECORATION/INVERTED scores (INVERTED = HIGH) so the condition is self-monitoring.
Principle: keep the evidence-based CATEGORICAL gates (regime ownership, dead-expectancy
stand-down, day governor, cost-floor, DTE, kill-switch); remove the continuous score
MAGNITUDES that have no predictive IC. No edge created; noise removed from sizing.

---

## 26. The Regime Organ — audit, three real bugs, and the standing rules (2026-08-03)

Triggered by a live check during market hours: **210 of 213 signals skipped
`RAE_ROUTER_STAND_DOWN`** on a day that was, in reality, quiet and rangebound — the
premium sellers' own regime. Nothing was at a trade cap, nothing was loss-locked, and
there were no errors in the log. Three independent defects, all now fixed and deployed.

### 26.1 The diagnostic method that found it (use this one)
Run the SAME `classify_intraday()` on **freshly-fetched real broker bars** and diff it
against what `market_regime_state` holds:

| Index | TRUTH (real bars) | APP believed |
|---|---|---|
| NIFTY | +0.13%, range 0.38% → **INSIDE_QUIET** | TREND_UP 0.90 |
| BANKNIFTY | +0.40%, range 0.67% → **RANGE** | TREND_UP 0.84 |
| SENSEX | **−0.14%**, range 0.50% → **INSIDE_QUIET** | HIGH_VOL_CHOP **1.0** |

**The classifier was correct every time; its INPUT was corrupt.** Do this diff BEFORE
touching any regime logic — the pure functions are not usually where the bug is.

The tell was **confidence 1.0**. Solving the confidence formula backwards gives
"range >= 2.8%" = ~2,200 SENSEX points on a 0.5% day. **An implausibly confident label
is evidence about the input, not about the market.**

### 26.2 Bug 1 — the capture aggregated ticks it should never have seen
`LiveIndexCapture.on_tick` had **no session filter and no price sanity check**.
- The feed connects whenever the token is refreshed — **08:32 IST that day, 43 min
  before the open**. Pre-open prints carry the PREVIOUS close, so `bars[0].open` became
  Friday's close and `ret_pct` turned **gap-inclusive**: a flat +0.13% session read
  +0.98% and classified TREND_UP at confidence 0.90.
- **`ltp in (None, "")` admits `0.0`.** One zero print — SENSEX, which BSE publishes
  before it starts computing the index — sets the day's low to zero, so `rng_pct` hits
  ~100% and the classifier returns **HIGH_VOL_CHOP at confidence 1.0**, the router's
  explicit stand-down.

Both were reproduced numerically *before* any code changed (sim 0.954 vs stored 0.904;
BANKNIFTY 0.858 vs 0.835; SENSEX 1.000 vs 1.0). `on_tick` now rejects non-positive
prices and anything outside the underlying's OWN segment window (SENSEX → BSE_FO 15:30,
not NSE's 15:40); `health()` counts both rejects.

**Why it hid:** the EOD store looked perfect — Friday's file was 375 clean rows starting
exactly 09:15 — because on earlier days the feed happened to connect AFTER 09:15. Only
the LIVE path reads the raw buffer. **A clean store does not prove a clean live path;
they read different things.**

**Related, same root:** the flush wrote every bar under the FLUSH date, so post-close
ticks accumulating after the EOD flush landed in the next file —
`index_1m/underlying=NIFTY/date=2026-08-01.csv.gz` (a **Saturday**) held 29 bars stamped
`2026-07-31T15:35..16:03` at a frozen price. Bars are now filed under their OWN date.

### 26.3 Bug 2 — `owned_regimes` was decorative; two live strategies could never trade
Ownership was decided **solely** by matching the role name against
`regime_taxonomy.REGIME_OWNER`. Two live strategies whose roles are absent from that map
therefore stood down in **ALL SIX** regimes:
- **`slow_premium_hte`** — the hold-to-expiry sleeve seeded 2026-08-03 (§25.5). Seeded,
  armed, and incapable of trading.
- **`tail_hedge`** — which *declared* it owned all six.

`visual_config.options.owned_regimes` was set on every seeded specialist and **nothing
read it** — the third instance of the decorative-config trap (`target_dte_days` §21.5,
`short_delta` for single-leg). `route()` now takes `owned_regimes=` and `structure=`, and
the declared set is authoritative when present; the role map remains the fallback for
untagged/legacy rows. Junk (empty list, unknown labels, non-iterable) falls back rather
than opening or closing everything.

**Declaring a regime is NOT a bypass.** The hard vetoes still run first, and the
chop/EVENT veto is now decided on **economics rather than a hardcoded role name**: a
structure that COLLECTS premium (`credit_spread`) can never trade HIGH_VOL_CHOP/EVENT —
the fat tail is against it — while a defined-risk BUYER that declares ownership may,
because expansion is what it is bought for. That generalises the `long_vol` carve-out
(which keeps its explicit name check) and is what lets a tail hedge be on during the
chop/EVENT it exists to cover.

Verified with a full before/after matrix: **the seller rows are byte-identical**; only
the two dead strategies changed, plus `inside_mean_revert` gaining its declared RANGE.

### 26.4 Bug 3 — the coarse regime called an overnight GAP an intraday TREND
`compute_regime_from_data` gated TREND on the return from the **previous close**, so a
gap alone satisfied it for the whole day. NIFTY gapped +0.85% and then went nowhere:
**37 of 41 ticks that session were labelled TREND_UP** (BANKNIFTY 33/41, SENSEX 34/41)
on session moves of +0.04% / +0.20% / **−0.23%**. That label is the conservative
cross-check the router applies to a RANGE fine-read, so a gap-and-flat day vetoed every
seller a second time.

TREND now gates on the **session move** (today's open → now); **CRASH/MELTUP keep the
gap-inclusive number** — a −1.5% gap-down IS a crash day regardless of what happens
next, and that fat-tail guard must stay conservative. `intraday_return_pct` keeps its
prev-close meaning for every existing consumer; `session_return_pct` is reported beside
it. Hysteresis (`REGIME_TREND_EXIT_BUFFER_PCT`, 0.15) stops the label flipping while
price sits on its VWAP — SENSEX flipped TREND_UP/RANGE **16 times in 20 minutes**.

**Validated on 18 real trading days — it is not "fewer trends", it is correct ones:**
- removes false trends on gap-then-flat days (07-10, 07-14, 07-27, 07-28),
- **adds true trends it was blind to** (07-09, 07-13, 07-24) — days that gapped AGAINST
  the eventual move and then genuinely trended intraday, which the old net-of-gap number
  read as RANGE.

⚠️ Note 2026-07-10 — the day §18 was written around — now reads RANGE: its move was
almost entirely the gap. That is the correct read *for an intraday entry decision*; a
gap hurts positions already held, which is a different problem from whether to open one.

### 26.4b Bug 4 — the regime only refreshed when a strategy liked the setup
`update_regime` sat in the strategy-runner loop **below** the `not signals` /
`not last_sig` / duplicate / low-confidence `continue`s, so an underlying's regime was
recomputed only when one of its strategies emitted a VALID signal.

BANKNIFTY's only live strategy is the trend rider, which needs a fresh 30-bar breakout.
It fired at 10:05 IST, went quiet, and `market_regime_state.BANKNIFTY` stayed **frozen at
its 10:05 value for the rest of the session** (three hours stale) while NIFTY/SENSEX —
whose sellers signal every tick — refreshed every ~2 minutes. It was visible as
`session_return_pct: undefined` on BANKNIFTY alone after the §26.4 deploy: the new code
had never once run for it.

**Not cosmetic.** The same block detects a mid-session regime FLIP and tightens
against-regime positions, and the CRASH/MELTUP entry blocks read the cached label — so
both depended on the underlying's strategies being chatty. **A genuine intraday crash on
a quiet underlying would not have tightened its open positions.**

The regime is a property of the MARKET, not of whether a strategy liked it. It now runs
immediately after the candles are in hand (no extra broker round-trip), before every
gate. Pinned by a source-order test, the §25.4b approach — the invariant is positional
and the loop is not unit-testable in isolation.

### 26.5 Standing rules this produced
- **Diff the regime against real broker bars before believing it** (§26.1). Three of the
  last four regime incidents were bad input, not bad logic.
- **A guard written `x in (None, "")` does not exclude `0.0`.** Range/low/high fields are
  where a single zero does maximum damage.
- **A filter must be applied on every branch that appends to the same result** (§22.1)
  AND re-applied wherever the underlying object is rebuilt (§22.7).
- **Config that no code reads is a bug, not documentation.** When adding a field to a
  strategy template, add the reader in the same commit or do not add the field.
- **Ask which regime a gate's evidence came from** before applying it elsewhere (§25.4) —
  and ask whether a threshold measured gap-inclusive means what you think intraday.
- **Idle is still usually correct** (§23.4) — but "idle" and "structurally incapable of
  trading" look identical from outside. The role x regime matrix is how you tell them
  apart; regenerate it whenever a specialist role or `owned_regimes` changes.

### 26.6 Verified, and what is NOT claimed
Deployed and confirmed live: all three indices flipped to `INSIDE_QUIET`,
`RAE_ROUTER_STAND_DOWN` fell **210/213 → 2/9** (the remainder a legitimately off-regime
tail hedge), and two credit spreads opened. Test suite: **105 failed / 1022 passed vs a
105 / 1003 baseline** — the same 105 pre-existing failures, +19 new tests, zero
regressions.

**None of this creates edge.** It removes defects that made the book stand down on its
own regime and mis-measure the market. Every strategy still owes the §13.5 ladder, and
§26.4 in particular changes which days the trend riders fire — that is UNVALIDATED and
owes forward-paper evidence. `CORE_ENGINE_LIVE_ENABLED=false`.

### 26.7 Open items found in this audit, NOT fixed (founder call)
- **`RAE * Range Seller` requires `close > ma20 > ma50`** (an uptrend) while the router
  gates it to RANGE. The two filters pull opposite ways: on a true range the MA stack is
  near-random, so it fires on roughly half its own regime. Design tension, not a bug —
  changing it is an alpha change and needs evidence.
- **`IDX NIFTY VRP Call-Spread` has no rally guard** while its sibling QG-O4 explicitly
  refuses to sell calls into a rally (`close <= ma20`). Same structure, same risk, one
  protected. (Mitigated in practice: the dynamic selector picks the side from the chain.)
- **`bhavcopy_fo` is missing 2026-07-31** — the ingest cron skipped Friday. Newest is
  07-30, inside the 10-day RES2 staleness guard, so it fails open rather than lying.
- **Hermes `infra.process_restarts::backend` is HIGH at 14 occurrences** — worth its own
  look; the OOM / Edge-Lab class of §22.2.
- **NOT a bug (checked):** the VRP strategies' `direction` is the DIRECTIONAL VIEW, not
  the leg — `BUY/CE` (bullish) means *sell a put spread*, which is why the strategy named
  "Put-Spread" correctly opened a PE credit spread. Do not "fix" this.

---

## 27. Why the sellers lose — the exit engine, not the signal (2026-08-03)

Audit of the loss-making strategies. **Judged on the current geometry epoch only**
(every seller was re-cut 2026-07-30, so blending in older trades grades a shape that
no longer exists — §21.5). Post-re-cut: **n=28, WR 43%, −₹3,001.**

### 27.1 The finding — one rule closes 77% of all trades
Post-re-cut exit mix for the whole credit book:

| exit | n | avg | total |
|---|---|---|---|
| **spread-no-progress** | **30** | −₹127 | **−₹3,824** |
| spread-sl | 1 | −₹987 | −₹987 |
| intraday-squareoff | 5 | −₹113 | −₹564 |
| spread-tp | 2 | +₹269 | +₹539 |
| trail-lock | 1 | +₹329 | +₹329 |

**30 of 39 exits are `no_progress`, and only 3 trades in the entire sample ever reached
a profit exit.** The TP/SL geometry is nearly irrelevant — almost nothing survives long
enough to be judged by it.

### 27.2 The wrong logic — a bar that could not be cleared
`no_progress_exit` requires the peak to reach **8% of credit within 20 minutes**. That
8% was calibrated (§21.10) against the peak distribution of trades measured over their
**whole hold** — winners median-peak 29%, dead trades ~0.3%. It was then applied inside
a flat 20-minute window. **Those are different questions.**

Theta can only return `held / (dte × session)` of remaining time value:

| DTE at entry | decay available in 20 min | bar asked |
|---|---|---|
| 0–1 | 5.19% | 8% |
| 2 | 2.60% | 8% |
| 4 (n=10 real trades) | **1.30%** | 8% |
| 6 (n=9) | **0.87%** | 8% |

Measured on the 30 trades the rule actually closed: mean peak **3.40%** of credit and
**ZERO of 30 ever cleared 8%**. The rule was not separating dead trades from live ones
— it was cutting everything that had not been *directionally* lucky in its first 20
minutes. Note the mean peak (3.40%) was already **above** what decay could supply at
DTE 4–6, so these trades were outperforming pure theta and were cut anyway.

**Fix:** keep the bar, but do not JUDGE until enough decay has been available for the
bar to be clearable (`DYN_EXIT_NO_PROGRESS_REQUIRE_REACHABLE`, env-reversible). Replayed
against the 30 real trades at the 20-minute mark: **old rule cut 30/30, new rule cuts
0/30** — they are held and judged later (~109 min at 4 DTE, ~164 min at 6 DTE), near
expiry still ~20–30 min. Same defect class as §21.5/§22.3: a threshold measured under
one regime applied to another.

⚠️ **NOT claimed:** that holding these trades makes them profitable. The counterfactual
is unknowable — they will be judged later on evidence that can exist, and many will
still be cut. What is proven is that the bar was unreachable by construction.

### 27.3 The geometry needs a win rate the book has never shown
Every live seller runs **tp 0.25 / sl 0.60 → break-even WR 70.6%**; realised is 43%
post-re-cut (50–62% lifetime). Note the 07-30 retune (§21.9) made this **worse**: TP was
halved (0.50→0.25) while SL fell only a third (0.90→0.60), so break-even went
**64% → 71%**. Lowering the target without lowering the stop by at least the same ratio
mechanically raises the required win rate — the reachability argument for the TP change
was sound, but it was applied to one side of the ratio only.

**Left as a founder decision, not silently re-tuned** — n=28 is below the §13.5 floor and
re-cutting geometry on a thin sample is the treadmill (§13.4).

### 27.4 A latent bug the SEBI change armed
The spread square-off was a **single global minute derived from the NSE close** — 15:35,
which from 2026-08-03 is **five minutes AFTER the BSE close (15:30)**. A SENSEX spread
would have been closed against a stale mark in paper and rejected outright in live. The
old 15:25 value hid it because it preceded *both* closes; it armed itself the day the
session changed and had never yet fired. Square-off, force-close and the backstop sweep
are now all **per segment** (`session_times.spread_squareoff_minute_for`,
`segment_for_underlying`), and the backstop runs a BSE pass at 15:26 before the NSE pass
at 15:36. The reason string carries the real minute — the literal `intraday-squareoff-1525`
kept appearing long after the time moved, which made every exit-timing question
unanswerable from the ledger.

**Rule: any time derived from one exchange's close must be asked for per segment.**

### 27.5 Two new permanent probes (§19 — a bug caught once is caught forever)
- **`strategy.geometry_vs_realized_wr`** — compares break-even WR to the strategy's OWN
  realised WR, scoped to the geometry epoch, silent under 20 trades. The existing
  `static.reward_risk_geometry` compares against a FIXED 0.75 constant, which is exactly
  why it stayed silent: 70.6% sits just under the alarm. **A fixed threshold cannot know
  whether 71% is achievable — only the strategy's own record can.** Keep both: the static
  one fires on day zero with no trades, this one needs a sample.
- **`exec.squareoff_after_segment_close`** — flags any position closed after its own
  segment's close, so a future divergence (BSE following NSE, a muhurat session, SEBI
  phase 2) is caught by measurement rather than by someone remembering a constant.

### 27.6 Corrections made during this audit
- I first reported `spread-sl` overshooting its stop by up to 3.7×. **Wrong** — I had
  computed the stop as `sl_mult × credit` while the code uses `credit × (1+sl_mult)` as a
  spread VALUE, and I queried `sl_value` when the field is `spread_sl_value`. Measured
  correctly the overshoot is **1.0–1.2×**, i.e. normal close slippage, not a defect.
- The headline "break-even 71% vs achieved 50–62%" mixed geometry epochs. The
  epoch-correct number is **43% over n=28**. Both are stated above; the epoch-scoped one
  is the one that grades the current machine.
- **The new probe correctly finds nothing today** — every seller's post-re-cut sample is
  8–11 trades, under its 20-trade floor. That is §19 working (thin evidence → silence),
  not a broken probe.

`CORE_ENGINE_LIVE_ENABLED=false`. Nothing here creates edge.

---

## 28. The 2026-08-04 session — five defects, and a fix that shipped inert (2026-08-04)

Day: **−₹6,269.73 over 21 closed trades, WR 38.1%.** NIFTY expiry Tuesday; the tape ran
24591 (09:34, first captured bar) → 24648 high @09:52 → a five-hour grind to 24428 @13:58
→ 24615 close. **Three trades produced 94% of the loss.** Everything below is a defect
removed, not edge created. `CORE_ENGINE_LIVE_ENABLED=false` throughout.

### 28.1 THE ONE TO INTERNALISE — a fix can ship, be enabled, and do nothing
`9ba74e6` (the §27 no-progress reachability guard) was **inert from the moment it shipped.**
`expiry` is persisted as a bare date string (`"2026-08-06"`); `parse_iso_dt` returns a
NAIVE datetime for that; subtracting it from an aware `utcnow()` raises TypeError; a bare
`except` swallowed it; `dte_days` arrived as `None`; and the gate read
`if NO_PROGRESS_REQUIRE_REACHABLE and dte_days is not None` — so **None skipped the gate**
and restored the exact flat 20-minute window the fix existed to remove. All 8 spreads the
next day were cut at exactly 20 minutes for −₹2,337.

**It shipped WITH a test that guaranteed this**: `test_missing_dte_preserves_the_old_behaviour`
asserted that a missing DTE keeps the flat-window cut. Reasonable in the abstract —
catastrophic here, because in production the DTE was *always* missing. The test passed
every single day the guard did nothing.

Three standing rules from this:
- **An unresolvable input must disable the RULE, not the SAFEGUARD.** `x is not None` as a
  gate precondition is fail-OPEN; it is almost always the wrong default around a guard.
- **A bare `except` on a parse turns a type bug into a silent behaviour change.** The caller
  now uses `dte_policy.dte_from_expiry` (date-robust, IST) and logs a WARNING when DTE is
  still unresolvable, so the case is loud.
- **Before writing a test for the "input missing" branch, ask how often that branch fires in
  production.** If the answer is "always", the test is pinning the bug.

### 28.2 The five defects
1. **Expiry settled at a leg's last premium, not intrinsic.** The day hold-to-expiry first
   ran (§22.3: 0 of 283 ever had), 7 positions settled against a NIFTY tick the feed had
   frozen for 13 minutes either side of — o=h=l=c from 15:15, one print at 15:28, frozen
   again to the close. Re-pricing them where the feed had sat moves the day by **₹12,659**,
   twice the reported loss, and **nothing about the settlement was recorded** (`close_value`,
   leg `exit_price`, settlement price all null), so none of it was visible afterwards. At
   expiry an option IS its intrinsic value; a last premium carries phantom time value AND
   whatever the feed last printed. `position_monitor._expiry_settlement_marks` now prices
   both legs at intrinsic vs the underlying and persists `settlement_underlying` /
   `settlement_source` / per-leg intrinsics; falls back to leg marks and says so.
   *A later fresh WS connect reported `ltp=24614.9`, corroborating that today's settlement
   number was in fact right — the fix stands on structure and auditability, not on today's
   P&L being wrong.* NSE settles index options on the 15:10–15:40 VWAP, so an LTP is still
   the wrong instrument even with a healthy feed.
2. **No-progress guard inert** — §28.1.
3. **No book-wide per-trade rupee ceiling.** `IDX NIFTY Mean-Reversion Fade` carried
   `required_capital` 20,000 against 4,000–13,000 everywhere else, sized to FIVE lots, put
   **₹15,811** of defined risk on one 0-DTE debit spread and lost **₹8,077 = 129% of the
   day's entire loss**. `spread_builder.cap_lots_by_risk` (`MAX_RISK_PER_TRADE_RUPEES`,
   default 8,000) now trims at both sizing call sites; the row's own budget was also
   migrated to 8,000.
   **It is NOT inside `lots_for_risk`** — that is a pure budget→lots function allowed to
   return 0 (callers floor at 1), and a ceiling that can return 0 would have stood the
   SENSEX sellers (₹12.9k/lot) and the HTE sleeve (₹30.7k/lot) down entirely. An existing
   invariant asserts exactly that. **It floors at 1 lot: it bounds multi-lot SCALING, not
   the absolute risk of one wide-winged lot** — refusing that is the cost-floor's job.
4. **The regime cross-check only fired on RANGE.** The fine read was **INSIDE_QUIET on 19 of
   21 entries** while the coarse organ read TREND_DOWN through the whole midday slide, and
   INSIDE_QUIET was treated as an "affirmative detection" and trusted. Six SENSEX put-spread
   entries went through for **−₹3,454 = 85% of that strategy's loss**, selling puts into the
   drop. The test is not *is the label affirmative* but **does this label authorise a
   seller** — RANGE and INSIDE_QUIET both do. Extended to every seller-permissive label,
   **scoped to premium-COLLECTING structures**: routing a defined-risk BUYER through it
   subjects it to the trend PRECISION gate (`confidence < 0.9 → likely fakeout, stand
   down`), which would turn "the coarse organ suspects a downtrend" into a reason to switch
   the crash insurance OFF. The original RANGE branch keeps its unscoped behaviour.
5. **The tail hedge was a 0-DTE day-trader.** `expiry_offset: 0` and no DTE window meant
   "nearest expiry" = same-week weekly, so it bought **0-DTE crash insurance** at a 0.5%-OTM
   strike (not "far-OTM"), contradicting its own `max_hold_days: 8`. It used all three daily
   entries: +₹7,176 on the slide (profit-lock trailing out of a +₹10,107 peak — worth
   ₹10,181 vs holding), then two re-entries near the LOW giving back ₹6,187. Now
   `min/max_dte_days 5–15` (honoured by `select_expiry`, §25.4b) and `max_trades_day: 1`.
   Note its `daily_loss_limit: 650` never bound because **hold-to-expiry losses do not
   realise until settlement** — any daily-loss governor is structurally blind to such a
   sleeve.

### 28.3 Two more found re-auditing the Hermes findings (24 open → 10)
- **`static.reward_risk_geometry` filed HIGH against both hold-to-expiry sleeves** (QG-O1
  ×12, HTE ×6) claiming they need an 88.9% win rate. They have **no TP/SL** — both correctly
  OMIT `credit_tp_frac`/`credit_sl_mult` (§25.5) — and the probe substituted the global env
  defaults for the missing fields, inventing a reward:risk that does not exist, on the two
  strategies that were the day's only clean winners. Now skips `exit_mode="expiry"` /
  `hold_to_expiry`.
- **`exec.specialist_regime_fit` compared the COARSE `regime_at_entry` against FINE
  `owned_regimes`** — different taxonomies (coarse has no INSIDE_QUIET; fine has no
  CRASH/MELTUP). 14 occurrences of pure noise, and it could never see a real fine-regime
  violation. Now judged on `regime_fine_at_entry`.
- **This is the THIRD instance of one class** (after `static.cost_floor` measuring gross
  credit against a bankable-profit law, §21.5). **Standing rule: a probe and the thing it
  judges must share a taxonomy AND an arithmetic — check which field the enforcement point
  actually gates on before writing the comparison.**
- **Fine-regime confidence is now damped when the capture missed the open.** Every intraday
  feature is anchored on `bars[0]` — `ret_pct`, `efficiency`, the opening range — so a late
  feed silently re-anchors the whole day, and `maturity` cannot detect it because it counts
  bars: **26 bars from 09:34 look exactly as mature as 26 bars from 09:15.** The token
  expired ~03:35 IST and the feed returned at 09:34, so 19 minutes including the true open
  were missing from every regime read of the day. Bars cannot be recovered after the fact,
  so the honest response is to be less sure; the router already gates on confidence.
  `REGIME_TRUNCATED_OPEN_TOLERANCE_MIN` (3), raw confidence and lost-minute count persisted.
- New probes: **`exec.expiry_settlement_integrity`** (a settlement that did not price at
  intrinsic) and **`exec.regime_organ_disagreement`** (it measured 19/21 = 90% on 08-04
  automatically — the thing that took manual digging to find).

### 28.4 The day, per strategy (for the record)
Winners: QG-O1 +₹3,884 (3, 100%), HTE +₹2,664 (2, 100%) — **both hold-to-expiry, the day's
only clean performers, and together the reason the day was not −₹12.8k**; tail hedge +₹989
(3, 33%); IDX NIFTY VRP call-spread +₹42.
Losers: IDX NIFTY Mean-Reversion Fade −₹8,077 (1 trade); IDX SENSEX VRP put-spread −₹4,068
(10, 10% WR); RAE SENSEX Range Seller −₹1,704 (1).
Exits: `spread-sl` 3 (−₹11,464) · `spread-no-progress` 8 (−₹2,337) · `expiry-settlement` 7
(+₹361) · `profit-lock-trail` 1 (+₹7,176) · squareoff 2 (−₹5).
**All 12 SENSEX trades were PE credit spreads.** The §21.6 contract dedup blocked 63 signals
correctly, but it keys on `(underlying, option_type, expiry, strike)` — it stops the
identical contract and **permits the same directional bet one strike over**. On a trending
day that is one position at 8× size wearing eight names. Not yet fixed; founder call.

### 28.5 Test-suite note
Full suite 9 failed → 10 failed / +17 passing. Verified NOT a regression: baseline and HEAD
are **identical** run per-file and with env pinned (both clean at `SPREAD_COST_FLOOR_MULT=3.0`,
both fail the same 3 at 1.0). The delta is the §25.5 cross-file module-reload contamination,
which is order-sensitive and which any new test file perturbs. `test_session_fixes_08_04.py`
restores both reloaded modules in an autouse fixture so it adds nothing to that debt.

### 28.6 Still open (founder call, not bugs)
- **`infra.feed_down_at_open` (CRITICAL)** — the token expires ~03:30 IST and nothing
  auto-reconnects; today's manual reconnect landed 09:34. **The single highest-value
  operational fix: reconnect before 09:15.** Damping now limits the damage, not the cause.
- `strategy.persistent_live_loss` ×3 (idx-sensex-putspread −₹4,918/42, rae-range-seller-sensex
  −₹2,787/16, QG-O4 −₹662/34) — strategy-edge questions, not pipeline bugs.
- `score_not_predictive`: `regime_confidence` INVERTED (IC −0.18), `contract_edge_score` and
  `edgemath_conviction` DECORATION — already neutralised for sizing by `SCORE_SIZE_NEUTRAL`
  (§24.1); they remain as monitoring signals.
- **Directional concentration** (§28.4) and the `blocked_margin` float residue (6.2e-11).

---

## 29. Upstox daily token — the scheduled-approval flow (2026-08-04)

Closes the CRITICAL `infra.feed_down_at_open` leak (§28.6). The Upstox access token
**expires at 03:30 IST every day regardless of when it was issued**, and Upstox
issues **no refresh token** to this app — the code exchange is a token-only grant,
and `db.broker_keys` has never held a `refresh_token`. So it must be re-obtained
daily. On 2026-08-04 that landed at 09:34, and because every intraday regime
feature is anchored on the FIRST captured bar, the whole day's regime read was
anchored 19 minutes late.

### 29.1 Why NOT automated TOTP login
Upstox staff state plainly: *"login shouldn't be automatically done and user must
login daily manually"*, citing SEBI; and *"There is no option for that... You need
to do it daily as per SEBI's instructions."* There is no extended/long-lived token
(unlike some competitors). Storing the account password + TOTP seed on an
internet-facing VPS to save one tap would violate the broker's terms, put the real
brokerage account at risk, and cut directly against the SEBI PMS/AIF registration
path in §10. Third-party packages exist (`upstox-totp`); they are not used here.

### 29.2 The sanctioned flow (implemented)
```
08:45 IST  scheduler POSTs /v3/login/auth/token/request/{client_id}
           body {"client_secret": ...}   [skipped if the token is already fresh]
   ->      Upstox pushes an in-app / WhatsApp approval prompt
   ->      founder taps approve on their phone
   ->      Upstox POSTs the token to our notifier webhook, which applies it
09:05 IST  CRITICAL log + db.app_alerts row if it still has not arrived
```
No credential is stored anywhere, the human approval SEBI asks for still happens,
and it happens 30 minutes BEFORE the open instead of 19 minutes after it.

| Piece | Where |
|---|---|
| Pure helpers + constants | `core/upstox_auth_request.py` |
| Trigger (auth'd) | `POST /api/broker/upstox/auth-request` |
| Webhook (PUBLIC) | `POST /api/broker/upstox/notifier` |
| Status / diagnosis | `GET /api/broker/upstox/token-status` |
| Scheduler blocks | `server.py` `_daily_scheduler_loop` (08:45 + 09:05) |
| Env | `UPSTOX_AUTH_REQUEST_ENABLED`, `UPSTOX_AUTH_REQUEST_MINUTE_IST` (525), `UPSTOX_AUTH_ALARM_MINUTE_IST` (545) |

### 29.3 The webhook is public and unauthenticated — by Upstox's requirement
Upstox mandates that the notifier endpoint not require authentication, and it does
**not sign the payload**. Anyone on the internet can POST to it. Two layers:
1. **Structural** (`validate_notifier_payload`, pure + 20 tests): `message_type`
   must be the access-token delivery, `client_id` must equal OUR api_key compared
   **constant-time**, token must be a plausible non-empty string. A missing
   `expected_client_id` **rejects** — "we could not determine who we are" must
   never mean "accept anything" (§28.1).
2. **Live verification**: the token is checked against Upstox's own profile
   endpoint *before* it is stored. A forged POST wastes one API call.
Verified live: an attacker `client_id` and a `{}` body are both refused, and a
payload carrying the REAL `client_id` with a forged token was rejected at layer 2
with the stored token left untouched. The token is never logged. The endpoint
always answers 200 — a webhook that 500s invites retries.

### 29.4 `apply_upstox_access_token()` is now THE single token-apply path
Shared by the OAuth callback and the webhook. This is load-bearing: **restarting
the ticker is what re-attaches the 1-minute capture tick listeners**, and a path
that stored a token without doing so is exactly the §22.7 root cause (the live
capture had never written a file in its life). Any future token source must go
through this function.

### 29.5 `token_is_fresh()` reasons about the 03:30 boundary, not elapsed hours
A token issued at 20:00 yesterday is **dead** at 09:15 today; one issued at 03:31
today is alive. Anything that measures age in hours gets this wrong twice a day.
Unparseable timestamp → stale (fail closed).

### 29.6 Operational
- **The notifier URL must be registered in the Upstox developer dashboard** as
  `https://quantgtrade.com/api/broker/upstox/notifier`. Upstox echoes the URL it
  has on file in the step-1 response; `token-status` reports it as
  `last_auth_request.notifier_url_on_file_at_upstox` next to
  `expected_notifier_url`. **That comparison is the only thing that distinguishes
  "approval not given" from "delivery had nowhere to go"** — check it first when a
  token does not arrive.
- **PROVEN END-TO-END 2026-08-04 23:35 IST.** Full chain exercised against
  production: request accepted (HTTP 200), Upstox echoed back exactly the
  registered notifier URL, founder approved on the phone, and the token arrived at
  the webhook 
  → `access_token_source="notifier"`, `verified_upstox_user_id=5RCA6V`,
  `feed_started=True`, V3 handshake + live tick (India VIX 12.19) one second later.
  `_attach_capture_listeners` runs on the same branch that logs *"Upstox ticker
  startup successful"*, and that line is in the trail — so the §22.7 capture
  re-attach is covered on this path too.
- Worst case remains strictly no worse than before: if a request fails or an
  approval is never given, the 09:05 alarm fires and the manual OAuth login path is
  completely unchanged.
- Manual re-send any time: `POST /api/broker/upstox/auth-request`.
- **Diagnostic gotcha:** checking `server._UPSTOX_GATEWAYS` via
  `docker exec ... python -c` reports an EMPTY cache — that spawns a NEW process,
  not the running uvicorn. In-process state must be read through an HTTP endpoint
  or inferred from the log trail, never from a separate interpreter.

### 29.7 The ORDER-UPDATE postback is a different webhook — and it was wide open
Upstox's app config has **three** URLs and they are easy to conflate:

| Field | Purpose | QuantG path |
|---|---|---|
| Redirect URL | OAuth return after interactive login | `/api/broker/upstox/callback` |
| **Notifier Webhook** | delivers the **access token** after approval (§29.2) | `/api/broker/upstox/notifier` |
| **Postback URL** | pushes **order / GTT updates** while trading | `/api/upstox/webhook/<secret>` |

The Postback path feeds `apply_broker_truth_event`, which writes broker TRUTH —
canonical status and fill price — onto `db.orders` keyed by `broker_order_id`.
It was **publicly reachable and unauthenticated**: an anonymous POST of
`{"order_id": …, "status": "COMPLETE", "average_price": …}` was accepted and
returned `status=FILLED` (verified against production 2026-08-04).

**Blast radius was 0 *only* because paper orders carry no `broker_order_id` and
`mode=live` count is 0.** It becomes real the day live trading is enabled — which
is exactly when the postback is wanted. Note also that the hole was open
**regardless of whether the Postback URL was configured in the dashboard**;
leaving that field blank never closed it, which is the kind of thing that reads as
"not enabled yet" and is really "exposed and unused".

Fix: a **secret path segment** (`UPSTOX_POSTBACK_SECRET`, in `backend/.env`, which
`docker-compose.yml` already passes via `env_file`). Upstox neither signs postbacks
nor allows auth headers, so an unguessable path is the standard defence. Once the
secret is set the **bare path is refused**, so enabling it CLOSES the old door
rather than adding a second one. With no secret configured the previous behaviour
is preserved (no silent breakage) but every hit logs a warning. Non-dict payloads
are rejected and processing errors return 200 with a reason — a 500 makes Upstox
retry a bad event forever.

Verified live: bare path → `unauthorized`; wrong secret → `unauthorized`; correct
secret → accepted; malformed body → HTTP 200, no 500. Rejected payloads are not
even written to `upstox_broker_events`, so the audit collection cannot be flooded.

**Standing rule: every unauthenticated inbound webhook needs a stated defence.**
The notifier has two layers (constant-time `client_id` + live token verification,
§29.3); the postback has the path secret. A webhook that "only Upstox knows about"
is not a defence — the URL is in a dashboard, a config file and this document.

---

## 30. Hold-to-expiry: how it works, and the DTE window that wasn't (2026-08-05)

### 30.1 The mechanism, end to end
Declared by ONE field: `visual_config.options.exit_mode = "expiry"`
(`position_monitor._strategy_holds_to_expiry`, cached per strategy).
`risk.exit_mode = "hold_to_expiry"` is set alongside it but the monitor does not
read that one.

```
intraday        ordinary spread row, marked live off the WS feed
15:35 / 15:25   monitor: if hold_to_expiry and not _spread_past_expiry(pos): return
15:36 / 15:26   EOD backstop: _hold_to_expiry_positions_due() -> skip unless a
                position has ACTUALLY reached expiry            <- was the §22.3 bug
overnight       row simply stays status=OPEN; nothing runs
restart         _subscribe_open_position_tokens_on_startup re-subscribes legs[]
expiry day      _spread_past_expiry -> close, exit_reason="expiry-settlement",
                priced at INTRINSIC vs the underlying (§28.2), settlement price
                and source persisted on the position
```

Four guards had to be exempted, each of which had silently made the sleeve
untradeable: the **kill-switch** (sleeve-scoped, skips defined-risk hold-to-expiry),
the **DTE stand-down policy**, the **§21.2 theta-reachability veto** (waived for
`exit_mode="expiry"` — theta gets its full life), and the **time recycle**
(`time_exit_minutes = 0`).

**Structural blind spot, unchanged:** hold-to-expiry losses do not realise until
settlement, so `daily_loss_limit` and every day-P&L governor are blind to such a
sleeve while it carries an unrealised loss. The bought wing is the real bound.

### 30.2 Evidence to date
8 expiry settlements ever: **2 genuinely held overnight** (opened 08-03, settled
08-04), 6 opened and settled the same day at 0 DTE.

### 30.3 The bug: `min_dte_days`/`max_dte_days` were decorative on the live path
There are **TWO** option resolvers, and §25.4b fixed the wrong one.
`server.py:18168` states it outright: *"This is the runner's real resolver —
`_resolve_option_for_strategy`, fixed separately, is only used by manual routes."*

The runner resolves through `InstrumentResolver`, whose
`_lookup_index_option_chain` called `get_option_chain(spot_key, **None**)` — take
whatever expiry Upstox returns by default — while `expiry_rule` was passed only
into a *diagnostic*. So on the path that actually trades, **both `expiry_offset`
and the DTE window did nothing.** The HTE sleeve was configured 5–15 DTE and
opened 0-DTE spreads: a same-session trade wearing a hold-to-expiry label, graded
against OOS evidence drawn from multi-day holds. Third instance of the
decorative-config trap after `target_dte_days` (§21.5) and `owned_regimes` (§26.3).

**Fix:** the expiry is resolved ONCE, before any fallback, via
`dte_policy.select_expiry` — the same function the manual path uses, so the two
cannot drift again.

**The load-bearing part is that it FAILS CLOSED.** An unsatisfiable window used to
fall through to `_search_index_option` (whose candidates start at `current_week`)
and then to the paper simulator, either of which returns precisely the near-expiry
contract the window exists to refuse — the fix would have looked applied and
changed nothing, which is how the original bug survived. Both fallbacks are now
skipped when a window is configured, and an expiry list that cannot be read is a
refusal rather than a guess. A strategy with **no** window is completely unaffected.

Verified live against the real chain: Tail Hedge and HTE now pick **2026-08-11
(DTE 7)**; all seven no-window NIFTY strategies still take the nearest expiry.

### 30.4 Surfaced, because invisibility is half of why it went unnoticed
- `RuntimeSettingsForm`: an **Expiry window (DTE)** min/max row, with copy stating
  the real behaviour — blank = nearest expiry (on a Mon/Tue that is the 0–1 DTE
  weekly); a set window **stands down** rather than substituting another tenor.
  Blank sends `-1`, which the route treats as CLEAR: a window that cannot be
  removed is a trap, because an unsatisfiable one stops the strategy trading.
- `StrategyCard`: a **HOLD-TO-EXPIRY** badge carrying the declared tenor, and a
  warning tooltip when a hold-to-expiry strategy has **no** window.
- `Positions`: expiry + DTE per row, 0 DTE highlighted (it settles today).
  `execution_state` now projects `expiry`/`dte` onto every open option position.
- Probe **`exec.hold_to_expiry_tenor`**: compares the DTE a strategy ASKED for
  against the DTE it actually opened at; silent when no window is declared.

### 30.5 Open, founder's call
**QG-O1 is hold-to-expiry with NO window**, so it takes the nearest expiry — on a
Monday or Tuesday that is the 0–1 DTE weekly. Its §15.5 OOS pass (QuantG's only
one) was for a *held* 3% OTM put spread, so the same label/tenor mismatch applies,
just unconfigured rather than ignored. Setting a window changes which contract it
trades, which is an alpha change and is not being done unasked. The new card badge
now shows this state rather than leaving it silent.

**Also noted, not traced:** QG-O1's 08-03 position had `spread_tp_value: 26.87`
stamped, crossed it comfortably, and did not exit — riding to settlement instead.
Better outcome here, but it suggests priced exits do not fire on hold-to-expiry
rows. Worth confirming the mechanism before calling it intended.

### 30.6 Test-suite note
10 failed / 1217 passed, env-pinned — identical failure set to before this work,
+41 passing. Separately fixed a **time-of-day flake**:
`test_reachability_vetoes_a_far_expiry_intraday_seller` asserted `dte_days == 6`,
adding six WHOLE days to today's date while the builder measures FRACTIONAL days
from `now`, so it read 6 just after 00:00 UTC and 5 for the rest of the day. The
suite passed in the morning and failed in the afternoon regardless of any change.
Confirmed pre-existing by running it at `05a86a5`.
