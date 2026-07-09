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
- **Time-based spread recycle** (`27933d9`, founder-directed): the spread exit engine had SL/TP/trailing-lock but **no time exit**, so a spread that drifts sideways sat until the 15:25 square-off, tying up the one-spread-per-strategy slot all day (QG-O11 took one morning scalp then idled). `position_monitor._process_spread_position` now closes a spread (reason `spread-time-exit`) after `visual_config.risk.time_exit_minutes` when no price trigger fired; price exits still take priority. Set post-`CREDIT_SPREAD_THETA_RISK` (which zeroes time_exit) in the per-strategy blocks: **QG-O11 45m** (scalp turnover), **QG-O1/O4 120m** (2h recycle). New cached helper `_strategy_time_exit_minutes`. The live book now exits on TP (50%/35% of credit) / SL (2x/1.5x) / trailing-lock / time-recycle — turnover-focused, not hold-and-hope. (Aside: "cleared manually via Ops console" sets a position to `CANCELLED` with realized_pnl 0 leaving unrealized unbooked — a separate manual-path wart, not the monitor.)
- **Midday entry cutoffs removed** (`884e0bd`, founder-directed): the book-wide `ENTRY_CUTOFF_IST` default flipped `1230`→`off` (`strategy_runner.py`), and the per-strategy code windows widened `13:00`→`15:00` (QG-O1/O2/O3/O4) and `12:30`→`15:00` (QG-O11) — the 12:30/13:00 blocks were starving the live book of the whole afternoon. 09:45 open-guard kept; per-position 15:25/15:10 square-off still prevents holding into close. **QG-O11** (scalper) was taking one morning trade then sitting because 12:30 blocked afternoon re-entry; it can now take its 3 daily scalps across the session (NOTE: afternoon window DEVIATES from its OOS geometry validated on 09:45–12:30; forward-paper judges). **IMPORTANT nuance:** this does NOT make QG-O1 trade — QG-O1 is independently blocked by its **RES-2 gate** (`res2_gate:true`): it only sells when IV−RV is rich AND regime=RANGE; on a TREND_UP day with realized>implied vol (e.g. IV 14.2 < RV 32.6) every signal is `RES2_GATE_BLOCKED` by design. Time-cutoff removal helps QG-O4/O11 (no vol gate), not QG-O1.
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
| Capture | `core/options_minute_capture.py` | forward live tick→1-min bar aggregator + store flush (feed wiring NOT attached yet) |
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

### 14.5 Daily forward-capture health checklist (once IMD-04 is wired)
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
