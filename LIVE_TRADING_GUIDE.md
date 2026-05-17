# QuantG Live Trading: Capacity Analysis & Startup Guide

## Your Hardware Specs
```
Laptop: Desktop-5D0PJ31
CPU: 4 Logical Processors (likely 2 cores w/ hyperthreading)
RAM: 4 GB Total (3.99 GB usable)
Current Docker Usage:
  - Backend: 116.7 MB (6.28%)
  - MongoDB: 123 MB (6.62%)
  - Frontend: 6.7 MB (0.36%)
  - Total: ~250 MB / 1.815 GB limit
```

## Safe Strategy Limits

### Maximum Concurrent Strategies: 3-5 (Recommended: 2-3)

**Why this limit?**

1. **CPU Constraint** (4 logical processors)
   - Each strategy runs Python code to evaluate conditions
   - Default tick interval: 30 seconds per strategy check
   - With 5+ strategies, CPU usage spikes during evaluation phases

2. **Memory Constraint** (4 GB physical, ~1.8 GB Docker limit)
   - MongoDB takes ~120 MB
   - Backend takes ~110 MB
   - Each active strategy holds candle history in memory (~5-15 MB each)
   - Safe margin: Keep available RAM >500 MB

3. **Network I/O Constraint**
   - Each strategy needs live candle data from Zerodha every 30s
   - 5+ strategies = 5+ parallel Kite API calls every 30s
   - Risk: Rate limit (Zerodha: ~100 calls/min) or connection timeouts

4. **Database Constraint**
   - Each tick writes evaluations, signals, orders to MongoDB
   - 30-second intervals with 5 strategies = ~10 writes/min
   - MongoDB can handle easily, but disk I/O increases

### Performance Breakdown

```
Strategy Execution Timeline (per 30-second tick):

T=0s:   Load strategy settings (all live strategies)
        Get latest candles from Zerodha (parallel, ~2-4s)
T=4s:   Run Python evaluation logic (per strategy, ~0.5-2s each)
        Check risk limits, place orders if signal triggered
T=6s:   Write results to MongoDB (~0.5s)
T=6.5s: Wait for next tick

With 2 strategies:  ~6.5s per cycle = Safe, stable
With 3 strategies:  ~8-10s per cycle = Still OK, some buffer
With 5 strategies:  ~15-20s per cycle = RISKY, might miss next tick
```

## Recommended Setup for Live Trading

### 1. Maximum Strategies Configuration
```
- Run 2-3 strategies initially
- Each strategy on DIFFERENT symbols (reduces API calls)
- Tick interval: Keep at 30 seconds (do NOT reduce to <15s)
- Each strategy on different timeframes (1min, 5min, 15min)
```

### 2. Resource Monitoring Thresholds (WARNING LEVELS)

Stop trading immediately if any of these occur:

| Metric | Yellow Alert | Red Alert (STOP) |
|--------|-------------|-----------------|
| Backend RAM | 60% (116 MB) | 80% (145 MB) |
| MongoDB RAM | 75% (136 MB) | 85% (154 MB) |
| CPU (avg over 1 min) | 70% | 90%+ |
| Available RAM on laptop | <800 MB | <400 MB |
| Order latency (placement to execution) | >5s | >10s |

## Daily Startup Guide (Step-by-Step)

### Step 1: Pre-Market Checks (30 minutes before market open)

```bash
# 1a. Restart Docker (ensures clean state)
docker restart quantg-backend quantg-frontend quantg-mongo

# Wait 10 seconds for services to be ready
# Check services are healthy
docker ps --format "table {{.Names}}\t{{.Status}}"
# Output should show all 3 running (not restarting)
```

### Step 2: Open Terminal & Navigate to Project

```bash
# Open PowerShell or Command Prompt
# Navigate to your project
cd D:\Quant\QuantG

# Verify docker-compose.yml exists
ls docker-compose.yml
```

### Step 3: Check Database Connectivity

```bash
# Test MongoDB is accepting connections
docker exec quantg-mongo mongosh --eval "db.adminCommand('ping')"
# Expected output: { ok: 1 }

# Test backend can reach MongoDB
docker exec quantg-backend python -c "
from pymongo import MongoClient
client = MongoClient('mongodb://mongo:27017')
print('Database ping:', client.admin.command('ping'))
"
# Expected output: Database ping: {'ok': 1.0}
```

### Step 4: Verify Backend Health

```bash
# Check backend is responding
curl http://localhost:8000/api/

# Expected output:
# {"status":"ok","service":"QuantG API"}

# Check auth endpoints
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"your@email.com\",\"password\":\"yourpass\"}"

# Should return token (or "Invalid email or password" if no account)
```

### Step 5: Check Live Market Data Connection

```bash
# Open browser and go to: http://192.168.31.4:3000
# Login to your account
# Go to: API Keys section
# Add your Zerodha API credentials:
  - Enter your API key
  - Enter your API secret
  - Enter your access token (from Zerodha Web)
# Click "Save"

# You should see: "Status: Connected ✓"
```

### Step 6: Load Strategies & Test

```bash
# In browser, go to: Strategies page
# For each strategy you want to run live:
  1. Click "Test Run" to verify it works
     - Should show: "Data source: Zerodha day" or "Zerodha 5m"
     - Should show candles being fetched
     - Should show signals if conditions match
  
  2. Once test passes, click "Go Live"
     - Status should change from "active" to "live"
     - You should see "SCANNING" indicator
```

### Step 7: Monitor During Market Hours

```bash
# Option 1: Monitor in Browser
#   - Go to Strategies page
#   - Watch for "SCANNING" status (green pulse = actively checking)
#   - Watch "Last scan" column updates every 30s
#   - Watch "Signals fired" count increase if trades happen

# Option 2: Monitor Backend Logs
docker logs -f quantg-backend

# Look for lines like:
# INFO:     POST /api/strategies/{{sid}}/test-run HTTP/1.1" 200 OK
# INFO: Strategy evaluation for user_id={{uid}}: 5 signals fired
# Warnings like "Connection timeout" = BAD

# Option 3: Monitor Resources
docker stats

# Watch these:
# - "MEM USAGE" should stay <200 MB for backend
# - "CPU %" should not exceed 50% average
# - MongoDB should stay <150 MB
```

### Step 8: Daily Shutdown (After Market Close)

```bash
# 1. Go to Strategies page
# 2. For each "live" strategy, click "Pause"
#    - Status should change to "paused"

# 3. In terminal, gracefully stop containers
docker-compose -f D:\Quant\QuantG\docker-compose.yml down

# Wait 5 seconds for clean shutdown
# Output should show:
# Container quantg-backend Stopping
# Container quantg-frontend Stopping
# Container quantg-mongo Stopping
# Network quantg_quantg-network Removing
```

---

## Troubleshooting During Live Trading

### Problem: Strategies not executing (status stays "active", not "live")
```bash
# Check: Did you click "Go Live"?
# Check: Are there any error messages on the strategy card?
# Check: Backend logs
docker logs quantg-backend | grep -i error | tail -20

# Fix: Pause, then click "Go Live" again
```

### Problem: "Connection timeout" errors
```bash
# Check: Is Zerodha API connected?
# In browser: API Keys > Status should show "Connected ✓"

# Fix: Re-enter Zerodha credentials
# 1. Go to API Keys
# 2. Click "Disconnect"
# 3. Re-add your Zerodha API key/secret/token
# 4. Wait 10 seconds, should show "Connected ✓"
```

### Problem: Backend using >150 MB RAM
```bash
# This means strategies are holding too many candles
# Fix: Reduce number of live strategies to 1-2
# OR: Reduce lookback period in strategy code (from 250 to 100 candles)

# Emergency: Restart backend
docker restart quantg-backend
```

### Problem: "Order placement failed" or "Rate limit exceeded"
```bash
# This means too many API calls to Zerodha
# Fix: 
#   1. Pause 1-2 strategies
#   2. Wait 5 minutes (Zerodha rate limit resets)
#   3. Go Live with remaining strategies
#   4. Do NOT run >2 strategies on same timeframe
```

### Problem: Laptop becoming slow/freezing
```bash
# Your laptop RAM is getting full
# Check Docker memory usage:
docker stats --no-stream

# If MongoDB + Backend + Frontend > 400 MB:
#   1. Immediately stop all live strategies (click "Pause")
#   2. Restart Docker: docker restart quantg-backend
#   3. Reduce to 1 strategy
#   4. Consider adding more RAM to laptop (8GB recommended for 5 strategies)
```

---

## Performance Optimization Tips

### 1. Reduce Candle History
In your strategy code, change:
```python
# Current: 250 candles (uses ~10 MB memory)
data = kite.historical_data(...)

# Optimized: 100 candles (uses ~4 MB memory)
# Only fetch last 100 candles instead of 250
```

### 2. Use Different Timeframes
```
Strategy 1: 15-minute candles (fewer API calls)
Strategy 2: Hourly candles (even fewer API calls)
Strategy 3: 5-minute candles (only if needed)

Do NOT run 3x 1-minute strategies (too many API calls = rate limit)
```

### 3. Increase Tick Interval (if you can afford slighter delays)
In `strategy_runner.py`, change:
```python
TICK_SECONDS = 30  # Default: check every 30s
TICK_SECONDS = 60  # Slower: check every 60s (uses less CPU)
```

Then rebuild:
```bash
docker compose -f D:\Quant\QuantG\docker-compose.yml up -d --build
```

---

## When to Upgrade Hardware

You should upgrade your laptop if:

- You consistently hit red alerts (CPU >80%, RAM <400MB)
- Strategies miss ticks (backend logs show "Tick skipped")
- Order latency exceeds 5 seconds
- You want to run >3 strategies simultaneously

**Recommended upgrade: 8GB RAM laptop**
- Allows 5-8 strategies safely
- Better buffer for market spikes
- No more freezing

---

## Summary: Safe Live Trading Checklist

Before going live each day:

- [ ] Docker all 3 containers running (`docker ps`)
- [ ] Backend responds to API calls (`curl http://localhost:8000/api/`)
- [ ] MongoDB connected (`docker exec quantg-mongo mongosh --eval "db.adminCommand('ping')"`)
- [ ] Zerodha API connected (browser: API Keys > Status shows "Connected ✓")
- [ ] Each strategy "Test Run" passes successfully
- [ ] You're running 2-3 strategies MAX
- [ ] You have >500 MB free RAM on laptop
- [ ] Backend <150 MB memory, CPU <50% average
- [ ] You have monitoring open (browser or `docker stats`)

---

**MOST IMPORTANT: Start small. Run 1 strategy on paper trading first. Only go live once you're confident.**
