# QuantG — AI Agent Operating Manual

**Read this first. Every session. No exceptions.**

This is the canonical reference for all AI agents working on the QuantG algo-trading platform. It supersedes all other documentation.

---

## 1. What This App Is

QuantG is an NSE/BSE options algo-trading platform.
- **Backend**: FastAPI + Motor (MongoDB) + asyncio loops. Single file: `backend/server.py` (~15k lines). Additional routes in `backend/routes/`.
- **Frontend**: React (CRA) + Tailwind. Pages in `frontend/src/pages/`. No SSR.
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
| Execution snapshot (positions+orders for UI polling) | `backend/execution_state.py` |
| Upstox WebSocket V3 feed + REST orders | `backend/brokers/upstox_gateway.py` |
| Signal processing loop | `backend/signal_manager.py` |
| Strategy evaluation loop | `backend/strategy_runner.py` |
| Broker reconciliation (live only) | `backend/position_reconciler.py` |
| Frontend positions page | `frontend/src/pages/Positions.jsx` |
| Frontend orders page | `frontend/src/pages/Orders.jsx` |
| Frontend strategies page | `frontend/src/pages/Strategies.jsx` |
| Frontend ops/risk page | `frontend/src/pages/OpsConsole.jsx` |
| Frontend execution data hook (polls /execution/snapshot) | `frontend/src/hooks/useExecutionState.js` |
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
docker-compose restart backend
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
ssh -i C:\Users\MG\.ssh\codex_quantg_vps root@82.180.145.183 "cd /opt/QuantG && git pull origin main && docker-compose restart backend"
```

---

## 4. Key Technical Concepts

### Position Lifecycle
```
RESERVED → PENDING_OPEN → PENDING_BROKER → OPEN → FILLED → EXITING → CLOSED
```
- Monitor loop (`_mongo_position_monitor_loop`) queries only `{PENDING_BROKER, OPEN, FILLED}` — not EXITING/CLOSED.
- Always mark position EXITING atomically **before** placing exit order. Revert to OPEN on failure.

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
- NIFTY: 75, BANKNIFTY: 30 (verify in `core/market_domains.py`)
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
