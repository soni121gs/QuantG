# 🚀 QuantG Streamlined Git Deployment Guide
> [!IMPORTANT]
> This is your primary deployment runbook. Follow these exact 3 steps whenever you need to push new local features or hotfixes to your live website (`https://www.quantgtrade.com`).

---

## The 3-Step Deployment Workflow

### 1. Step One: Commit and Push from Your Local Laptop
Open your local terminal or PowerShell in `D:\Quant\QuantG` and run:
```bash
# 1. Add all modified backend and frontend files
git add .

# 2. Commit the changes with a clean message
git commit -m "feat: deploy updates to live site"

# 3. Push the changes to your remote GitHub repository
git push origin main
```
*(Note: If your remote branch is named `master` instead of `main`, replace `main` with `master` in the last command).*

---

### 2. Step Two: Update and Rebuild on Your Contabo VPS
SSH into your Contabo live trading VPS (`82.180.145.183`) as `root` and execute these commands:
```bash
# 1. Navigate to your live app folder on the VPS
cd /root/QuantG

# 2. Pull the newly pushed changes from GitHub
git pull

# 3. Stop the currently running containers cleanly
docker compose down

# 4. Rebuild the frontend and backend images cleanly (ignoring cache)
docker compose build --no-cache backend frontend

# 5. Spin up all services (Mongo, Caddy, Backend, Frontend) in detached mode
docker compose up -d

# 6. Verify all containers are up, running, and healthy
docker compose ps
```

---

### 3. Step Three: Hard Refresh & Load the New HFT Presets
1. Open your live website: **`https://www.quantgtrade.com`**
2. Perform a **Hard Refresh** on your browser using **`Ctrl + Shift + R`** (or `Cmd + Shift + R` on Mac) to clear out any cached assets and load the updated React design.
3. Log in to your live account.
4. Go to the **Strategies** page.
5. Click **"Install Presets"** (or **"Seed Defaults"**) in the top right to instantly load the new presets with the updated logic!

---

## Useful Diagnostic & Monitoring Commands

| Command | Purpose |
|---------|---------|
| `docker compose ps` | Check status of running containers |
| `docker compose logs --tail=50 -f backend` | Monitor live backend server logs and errors |
| `docker compose logs --tail=50 -f frontend` | Monitor live frontend server logs |
| `docker compose restart backend` | Quick-restart the backend API service |
| `docker compose down` | Safely stop all containers without deleting data |
| `docker stats` | View real-time CPU, RAM, and memory consumption |

---

**All files configured. Deploy with confidence.** 🚀
