# QuantG VPS Deployment Guide — Contabo (82.180.145.183)

## Pre-Deployment Checklist

Before deploying to your Contabo VPS, ensure you have:

- [ ] SSH access to your VPS (IP: 82.180.145.183)
- [ ] Docker and Docker Compose installed on VPS
- [ ] Zerodha API credentials (api_key, api_secret)
- [ ] Backend `.env` file with Zerodha keys
- [ ] All configuration files updated with VPS IP

---

## Step 1: Configure Zerodha in Your Kite App

1. Go to https://developers.kite.trade/apps
2. Click on your QuantG app
3. Set **Redirect URL** to:
   ```
   http://82.180.145.183:8000/api/zerodha/exchange
   ```
4. **Save** and note your **API Key** and **API Secret**

---

## Step 2: Update Backend Environment

Edit `backend/.env` on your VPS:

```bash
MONGO_URL=mongodb://mongo:27017
DB_NAME=quantg
CORS_ORIGINS=http://localhost:3000,http://localhost:8000,http://192.168.31.4:3000,http://192.168.31.4:8000,http://82.180.145.183:3000,http://82.180.145.183:80,http://82.180.145.183:443,http://82.180.145.183:8000
JWT_SECRET=sk-emergent-c9f7fFc3707322110B
EMERGENT_LLM_KEY=sk-emergent-b6a9f3c8e1d7a25f48c93b6e8d1a7f5c2e9b4d8a6f3e1c7b9d2a5c8f4e1b6d9a
```

---

## Step 3: Deploy with Docker Compose

SSH into your VPS and run:

```bash
cd /home/QuantG
docker compose up -d --build
```

Monitor startup:

```bash
docker compose logs -f backend
docker compose logs -f frontend
```

---

## Step 4: Verify Services

### Backend Health
```bash
curl http://82.180.145.183:8000/api/
```

Expected response:
```json
{"status":"ok","service":"QuantG API"}
```

### Frontend Health
```bash
curl http://82.180.145.183/
```

Should return HTML (React app).

### MongoDB
```bash
docker exec quantg-mongo mongosh --eval "db.adminCommand('ping')"
```

---

## Step 5: First Time Setup

1. **Open your browser** and go to:
   ```
   http://82.180.145.183:80
   ```

2. **Register** a new account or **login**

3. **Go to Broker Keys** (left sidebar)

4. **Step 1:** Save your Zerodha API credentials
   - API Key: [your key]
   - API Secret: [your secret]

5. **Step 2:** Copy the Redirect URL shown (confirm it matches Kite app settings)

6. **Step 3:** Click "Connect to Zerodha"
   - This opens Kite login → grant permissions → redirects back with request_token
   - Backend exchanges token for access_token
   - You'll see "Connected ✓"

---

## Step 6: Load Default Strategies

1. Go to **Strategies** (left sidebar)
2. Click **"Seed Defaults"** button
3. You'll see 10 pre-built NIFTY and SENSEX option strategies in draft mode

---

## Step 7: Test Before Live Trading

1. **Select a strategy** (e.g., "NIFTY Momentum EMA")
2. Click **"Test Run"**
   - Should fetch live NIFTY data from Zerodha
   - Should show signals if conditions match
3. If test passes → ready for live trading

---

## Step 8: Go Live Tomorrow

### Morning Checklist (Before Market Open)

```bash
# 1. Verify all containers running
docker ps

# 2. Check backend logs for errors
docker logs quantg-backend | tail -20

# 3. Verify MongoDB is healthy
docker exec quantg-mongo mongosh --eval "db.adminCommand('ping')"
```

### In Browser (9:00 AM IST)

1. Go to http://82.180.145.183:80
2. **Login** with your credentials
3. **Go to Broker Keys** → Click "Connect to Zerodha" (daily re-authentication required)
4. **Go to Live Readiness** page → Verify all checks pass ✓
5. **Go to Strategies** → Select one → Click **"Go Live"**
6. Watch **"SCANNING"** indicator pulse (green = actively checking for signals)
7. Monitor **"Last scan"** column — should update every 30 seconds

---

## Monitoring & Troubleshooting

### Check Logs in Real-Time
```bash
docker logs -f quantg-backend
```

### Restart Services
```bash
docker compose restart quantg-backend
```

### Emergency Stop All Strategies
```bash
docker compose down
```

### View CPU/Memory Usage
```bash
docker stats
```

---

## Zerodha OAuth Flow (Reference)

1. User clicks **"Connect to Zerodha"** on Broker Keys page
2. Frontend calls `GET /zerodha/login-url` → Backend returns Kite login URL
3. User logs into Kite → Kite redirects to:
   ```
   http://82.180.145.183:8000/api/zerodha/exchange?request_token=XXX&status=success
   ```
4. Backend exchanges `request_token` → gets `access_token`
5. Backend stores `access_token` in MongoDB
6. Frontend shows **"Connected ✓"**

---

## Key Endpoints (For Reference)

| Endpoint | Purpose |
|----------|---------|
| `GET /api/` | Health check |
| `POST /api/auth/login` | Login |
| `POST /api/auth/register` | Register |
| `GET /zerodha/login-url` | Get Kite login URL |
| `POST /zerodha/exchange` | Exchange request_token for access_token |
| `GET /strategies` | List all strategies |
| `POST /strategies/{id}/test-run` | Test strategy now |
| `POST /orders` | Place a manual order |
| `GET /positions` | View current positions |

---

## Quick Reference: Daily Workflow

```
9:00 AM IST
├─ SSH into VPS
├─ docker compose up -d --build (if not running)
├─ docker ps (verify all containers running)
└─ Open http://82.180.145.183:80

9:05 AM IST
├─ Login
├─ Broker Keys → "Connect to Zerodha"
├─ Verify "Connected ✓"
└─ Wait for market data to populate

9:10 AM IST
├─ Go to Strategies
├─ For each strategy you want to trade:
│  ├─ Click "Test Run"
│  ├─ Verify it fires signals correctly
│  └─ Click "Go Live"
└─ Monitor "SCANNING" indicator

3:30 PM IST (Market Close)
├─ Pause all live strategies
├─ View final P&L in Portfolio
└─ docker compose down (optional)
```

---

## Performance Limits (Your Setup)

Based on your Contabo VPS specs, safely run:
- **2-3 concurrent strategies** (max)
- **1-minute interval minimum** (do not go faster)
- **100-250 candle lookback** (less = faster)

If you hit CPU/RAM limits, Docker will auto-kill containers. Monitor with:
```bash
docker stats
```

---

**Ready to trade tomorrow?** All files are configured. Just deploy and verify health checks pass. Let me know if you hit any errors!
