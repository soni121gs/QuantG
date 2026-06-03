# 🔍 QuantG Staged Truth Audit & System Verification Report

This report documents the current active branch, commit, dirty state, version markers, docker compose status, and version mismatch risks for both local and VPS production environments.

## 1. Codebase Metadata
- **Current Git Branch:** `main`
- **Current Git Commit:** `6e0e2c3d275b30f96a5cc309ba81c80a22c705a7`
- **Workspace Dirty State:** Clean (no uncommitted changes)
- **Backend Version:** `12.0` (as defined in `server.py`)
- **Root Version File (`VERSION`):** `10.0.0`
- **Frontend Version (`package.json`):** `10.0.0`

## 2. Docker Compose Infrastructure
The single source of truth deployment stack runs on the following Caddy-proxy network:
- **`quantg-mongo`** (`mongo:6.0`): Stores strategy configurations, execution logs, user profile settings, and daily trade metrics.
- **`quantg-backend`** (`./backend`): Running FastAPI / Uvicorn, holds SQLite state ledger `/data/runtime_state.sqlite3`.
- **`quantg-frontend`** (`./frontend`): Multi-stage React application, served as a static build over proxy.
- **`quantg-caddy`** (`caddy:2.8-alpine`): Edge reverse proxy listening on ports `80`/`443` with automatic Let's Encrypt SSL management.

## 3. Stale Build & Version Mismatch Risks
- **Frontend/Backend Version Desync:** The frontend `package.json` and the root `VERSION` file are set to `10.0.0`, while the backend `server.py` defines `APP_VERSION` as `12.0`. This difference is tracked to prevent stale client configurations.
- **Docker Compose Cache Risk:** In hot-patch scenarios on the VPS, executing a basic `docker compose restart` or `docker compose up -d` will **NOT** trigger a rebuild of modified source files. Developers must explicitly run `docker compose build --no-cache` to bake local code modifications into images.
- **Stale Paper State Risk:** If paper trading is reset on a dirty database, lockups and old position reservations can prevent fresh signal execution. To mitigate this, a safer reset script is provided.

## 4. Verification and Security Guards (Audit Completed)
- **Live Safety Firewall:** Global safety checks injected into `_place_upstox_order` prevent live broker calls if `CORE_ENGINE_LIVE_ENABLED=false` or if `live_arm_state` is disarmed.
- **Quote Staleness Check:** Price quotes older than 60 seconds during live trading hours are discarded to avoid execution on stale prices, mapping cleanly to `STALE_PRICE` skipped signals.
- **Preflight Code Normalization:** Unified error code mapping: `STRATEGY_DISABLED`, `MARKET_CLOSED`, `INSTRUMENT_UNRESOLVED`, `PRICE_UNAVAILABLE`, `STALE_PRICE`, `CONFLICT_BLOCKED`, `RISK_BLOCKED`.
- **Automatic Test Validation:** 5 out of 5 unit tests passed successfully on `2026-06-03`.
