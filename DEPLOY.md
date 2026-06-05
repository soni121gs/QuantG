# 🚀 QuantG Unified Deployment & Operations Runbook

> [!IMPORTANT]
> This runbook is the **single source of truth** for running QuantG locally and deploying it live to your VPS production server (`https://www.quantgtrade.com`). All obsolete deployment scripts and duplicate manuals have been archived under `archive/deploy/` to prevent directory clutter and configuration conflicts.

---

## 💻 1. Local Development environment

To spin up QuantG locally on your laptop with full isolated services (MongoDB, FastAPI Backend, React Frontend):

### **Start Services**
```bash
# From the root directory D:\Quant\QuantG
docker compose up -d
```

### **Rebuild Local Changes**
If you modify backend or frontend code locally:
```bash
docker compose build --no-cache
docker compose up -d
```

### **Stop Services (Keeping Data)**
```bash
docker compose down
```

### **Hard Reset (Deletes Local Database & SQLite Ledger)**
```bash
docker compose down -v
```

---

## 🌐 2. Standardized VPS Deployment Pipeline (Local → Git → VPS)

Always follow this exact pipeline. **Do not use manual tarballs or zip bundles.** Pushing code to GitHub and pulling it on the VPS ensures a clean, identical codebase.

### **Step 1: Commit and Push from Your Laptop**
```bash
# 1. Open terminal in D:\Quant\QuantG
git add .

# 2. Commit with a meaningful message
git commit -m "feat: deploy stable patches to live site"

# 3. Push to GitHub main
git push origin main
```

### **Step 2: Update and Rebuild on Your Contabo VPS**
SSH into your Contabo VPS (`82.180.145.183`) as `root` and run:
```bash
# 1. Navigate to the single source of truth app folder
cd /app/quantg/QuantG

# 2. Pull the latest commits cleanly
git pull origin main

# 3. Rebuild the frontend and backend cleanly
docker compose build --no-cache backend frontend

# 4. Spin up all services (including Caddy and MongoDB)
docker compose up -d

# 5. Confirm all containers are Up and healthy
docker compose ps
```

---

## 🔒 3. HTTPS & Reverse Proxy (Caddy Notes)

QuantG uses **Caddy** as the edge web server on the VPS to automatically manage free Let's Encrypt SSL/TLS certificates and route traffic.
*   **Active Ports:** Only ports `22` (SSH), `80` (HTTP), and `443` (HTTPS) are exposed through the firewall (`ufw`).
*   **Routing:** Caddy listens on `80/443`, requests SSL for `quantgtrade.com` / `www.quantgtrade.com`, and reverse-proxies requests internally to `frontend:80` (which in turn routes `/api/` requests internally to `quantg-backend:8000`).
*   **No Exposed Ports:** Ports `8000` (Backend API) and `27017` (MongoDB) are internal-only and **blocked by the firewall** for premium security.

---

## 🔄 4. Rollback and Disaster Management

If a deployed update introduces a critical bug, roll back instantly on the VPS:

```bash
# 1. SSH into the VPS and navigate to the folder
cd /app/quantg/QuantG

# 2. Revert the working directory to the last known stable commit (e.g. HEAD~1)
git reset --hard HEAD~1

# 3. Rebuild and restart the services using the rolled-back code
docker compose build --no-cache
docker compose up -d
```

---

## 📊 5. Logging and Diagnostic Commands

Monitor container logs in real time from the VPS:

| Command | Purpose |
| :--- | :--- |
| `docker compose ps` | Check if backend, frontend, mongo, and caddy are up and running. |
| `docker compose logs -f backend` | Monitor live strategy triggers, API calls, and Upstox connection logs. |
| `docker compose logs -f caddy` | Monitor live SSL certificate issues or incoming request traffic. |
| `docker compose restart backend` | Perform a ultra-fast backend restart without touching databases. |
| `docker stats` | Monitor real-time memory and CPU utilization. |

---

## 🚨 6. Emergency Recovery Procedures

### **Scenario A: Port Clash / "Address already in use"**
If you see port binding errors, another container is holding port 80/443:
```bash
# Stop containers across all potential legacy folders
cd /root/QuantG 2>/dev/null && docker compose down || true
cd /opt/quantg 2>/dev/null && docker compose down || true
cd /app/quantg/QuantG 2>/dev/null && docker compose down || true

# Kill any stray docker containers forcefully
docker rm -f quantg-frontend quantg-backend quantg-mongo quantg-caddy 2>/dev/null || true

# Prune system and restart cleanly
docker system prune -f
cd /app/quantg/QuantG && docker compose up -d
```

### **Scenario B: Environment File (.env) Missing**
`.env` is ignored by git to protect secrets. If it gets deleted, restore it inside `/app/quantg/QuantG/backend/.env` with your laptop’s secrets:
```env
MONGO_URL=mongodb://mongo:27017
DB_NAME=quantg
CORS_ORIGINS=https://www.quantgtrade.com,https://quantgtrade.com,http://82.180.145.183,http://localhost:3000,http://localhost:8000
JWT_SECRET=sk-emergent-c9f7fFc3707322110B
CREDENTIAL_ENCRYPTION_KEY=2X5KgDFov9ua19V+IrleQKgd37kzqTMPt1tb8vcuot0=
GEMINI_API_KEY=your_api_key_here
GEMINI_MODEL=gemini-2.5-flash
GEMINI_TIMEOUT_SEC=20
```

---

## 💡 7. Morning Pre-Market Checklist
Before trading hours start, open **`https://www.quantgtrade.com`**:
1. Perform a **Hard Refresh** (`Ctrl + Shift + R`) to force clean dashboard assets.
2. Log in and navigate to **Broker Keys**.
3. Daily broker authentication: Ensure Upstox keys are active and connected.
4. Verify system connection inside **Ops Console**.
5. Switch system mode to **PAPER** or **LIVE** as desired.

---

## 🔍 8. Uptime, Diagnostics, and Live Readiness

QuantG includes deep diagnostic APIs and verification utilities to ensure full operational awareness:

### **Diagnostics Endpoints**
*   **Version Info (`GET /api/version`):** Unauthenticated endpoint returning API version, git commit, branch, dirty status, start time, and server uptime.
*   **Runtime Details (`GET /api/ops/runtime`):** Authenticated diagnostics detailing environment settings, active loop task statuses (runner, signal manager), Upstox connection state, and frontend version.
*   **Live Readiness (`GET /api/trading/live-readiness`):** A pre-flight dry-run check verifying key parameters before live trading can start (verifies API credentials, WS feed, margins, global switches, and reconciliation).

### **Diagnostic CLI Scripts**
Inspect your system directly from the backend directory using the stabilization scripts:
```bash
# 1. Inspect all strategies, candles freshness, halt reasons, and signal status
python backend/scripts/strategy_doctor.py

# 2. Audit overall trading state, active positions, skips, and risk switches
python backend/scripts/audit_trading_state.py

# 3. Safely reset paper state, clearing paper orders, positions, and wallet balances (requires --confirm)
python backend/scripts/reset_paper_state_safe.py
```
