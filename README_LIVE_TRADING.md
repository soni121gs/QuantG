# QuantG - Live Trading Setup Complete ✓

## Your Capacity Summary

**Hardware:** 4GB RAM, 4 CPU cores (2 cores + hyperthreading)
**Safe Limit:** 2-3 strategies maximum
**Architecture:** Backend 30-second tick cycle, Zerodha API integration

## Quick Reference

### 3 Simple Ways to Start Each Day

#### Option 1: Automated (Easiest)
```bash
# Double-click this file:
D:\Quant\QuantG\START.bat

# It automatically:
# - Restarts all containers
# - Checks MongoDB connection
# - Verifies backend is healthy
# - Shows you the status
```

#### Option 2: Manual (If script fails)
```bash
# Open PowerShell in D:\Quant\QuantG and run:
docker-compose -f docker-compose.yml down
docker-compose -f docker-compose.yml up -d --build

# Wait 10 seconds for all containers to start
docker ps  # Verify all 3 showing as "Up"
```

#### Option 3: One-Liner
```bash
cd D:\Quant\QuantG && docker-compose up -d
```

---

## Daily Operating Procedure

### Pre-Market (9:14 AM - 5 minutes before market open)

1. **Click:** `START.bat` in D:\Quant\QuantG folder
2. **Wait:** For all 3 containers to show "Up"
3. **Open:** http://192.168.31.4:3000 in browser
4. **Login:** With your credentials
5. **Check:** API Keys section → Zerodha status = "Connected ✓"

### Market Hours (9:15 AM - 3:30 PM)

1. **Go to:** Strategies page
2. **For each strategy:**
   - Click "Test Run" → verify it shows candles and signals
   - Click "Go Live" → status changes to "live"
3. **Open:** `MONITOR.bat` in separate window
4. **Watch:** 
   - "Last scan" updates every 30 seconds
   - "Signals fired" counter increases if trades happen
   - CPU & RAM stay within limits (see red alerts below)

### Market Close (3:30 PM)

1. **Go to:** Strategies page
2. **For each strategy:** Click "Pause" 
3. **Wait:** Status changes to "paused"
4. **Click:** `STOP.bat` to gracefully shutdown
5. **Done!** All data saved to MongoDB

---

## Resource Limits (RED ALERTS = STOP IMMEDIATELY)

| Metric | Stop When | What To Do |
|--------|-----------|-----------|
| Backend RAM | >150 MB (8%) | Pause 1 strategy, restart backend |
| MongoDB RAM | >150 MB (8%) | Pause all strategies, check disk |
| CPU average | >70% (3/4 cores) | Pause 1 strategy |
| Laptop Free RAM | <400 MB | Close other apps, pause strategies |
| Order latency | >10 seconds | Stop trading, check Zerodha connection |

**Command to check resources:**
```bash
docker stats --no-stream
```

---

## Strategy Configuration

### ✓ DO THIS (Safe Configuration)
```
Strategy 1: NSE NIFTY50 + 15-min candles + RSI-based
Strategy 2: NSE BANKNIFTY + 1-hour candles + MACD-based
Strategy 3: NSE FINNIFTY + 30-min candles + Bollinger Band-based

TOTAL: 3 strategies, different symbols, different timeframes
```

### ✗ DON'T DO THIS (Will Cause Failures)
```
5 strategies all on NSE NIFTY50 1-minute candles
  → Too many API calls = rate limit exceeded
  → Backend CPU spikes
  → Orders may fail
```

---

## Troubleshooting

### Scenario 1: "Strategies not going live"
```
1. Check: Did you click "Go Live" button?
2. Check: Are there red error messages on the card?
3. Fix: Click "Pause" then "Go Live" again
4. If still fails: Check backend logs
   docker logs quantg-backend | tail -50
```

### Scenario 2: "Zerodha shows disconnected"
```
1. Go to: API Keys section in browser
2. Click: "Disconnect"
3. Re-enter: Your API key, API secret, Access token from Zerodha
4. Wait: 10 seconds for connection to establish
5. Should show: "Connected ✓"
```

### Scenario 3: "Orders not executing / Latency >5 seconds"
```
1. Check: Zerodha API connection (above)
2. Check: Your laptop internet connection
3. Reduce: Number of live strategies to 1
4. Increase: Tick interval (if you can tolerate 60s instead of 30s)
   Edit D:\Quant\QuantG\backend\strategy_runner.py line 29:
   Change: TICK_SECONDS = 30
   To: TICK_SECONDS = 60
   Then: docker compose up -d --build
```

### Scenario 4: "Laptop freezing / RAM full"
```
IMMEDIATE: 
1. Go to: Strategies page
2. Click: "Pause" on ALL live strategies
3. Wait: 10 seconds for system to recover

MEDIUM-TERM:
1. Run: MONITOR.bat to check resource usage
2. If backend + MongoDB >300 MB: 
   - You're running too many strategies
   - Reduce to 1-2 strategies
   - Consider upgrading to 8GB RAM laptop

3. Restart everything:
   - docker-compose down
   - Wait 5 seconds
   - docker-compose up -d
```

### Scenario 5: "Backend keeps crashing / exiting"
```
Check logs:
docker logs quantg-backend

Common causes:
1. Out of memory: Reduce strategies to 1
2. Database connection lost: Restart MongoDB
   docker restart quantg-mongo
   
3. Python error in strategy: Check your strategy code
   Look for error messages in logs

4. Complete restart:
   docker-compose down
   docker-compose up -d --build
```

---

## Files in D:\Quant\QuantG

```
START.bat              → Click daily to start trading
STOP.bat               → Click after market to shutdown safely
MONITOR.bat            → Click during market to watch resources
LIVE_TRADING_GUIDE.md  → Detailed documentation (this file)

docker-compose.yml     → Container configuration
.env                   → Environment variables (backend URL, CORS, etc)

backend/               → Backend FastAPI code
  server.py            → Main API server
  strategy_runner.py   → Background task (30-second tick loop)
  requirements.txt     → Python dependencies

frontend/              → React frontend code
  .env                 → Frontend environment (backend URL)
  src/                 → React components

data/                  → MongoDB data persists here
```

---

## Performance Tuning (Optional)

### If you want to run 4-5 strategies safely:

1. **Increase tick interval** from 30s to 60s:
   ```bash
   # Edit backend/strategy_runner.py
   # Change line 29: TICK_SECONDS = 30 to TICK_SECONDS = 60
   # Rebuild: docker-compose up -d --build
   ```

2. **Reduce candle lookback** in each strategy from 250 to 100:
   ```python
   # In your strategy code, change:
   # data = kite.historical_data(..., days=250)  # Old
   # to:
   # data = kite.historical_data(..., days=100)  # New
   ```

3. **Upgrade laptop** to 8GB RAM (best solution)

---

## Live Order Placement Details

### How Orders Work

1. **Tick fires** (every 30 seconds)
2. **Strategy evaluates** (0.5-2 seconds per strategy)
3. **Signal generated?** (BUY/SELL)
4. **Risk checks** (position size, daily loss, etc.)
5. **Order placed** (Zerodha API call, 1-2 seconds)
6. **Order confirmed** (written to MongoDB, shown in browser)

### Total latency: 2-5 seconds from signal to order execution

### What Happens If:

| Event | Outcome |
|-------|---------|
| Strategy running, signal fires | Order placed within 5 seconds |
| Zerodha API timeout | Error logged, strategy retries next tick (30s later) |
| Risk check fails (e.g., max daily loss hit) | Order REJECTED, signal logged but not executed |
| Network disconnects | All pending orders cancelled, try to reconnect |
| Backend crashes | All live strategies stop, but positions remain open |

---

## Safety Checklist Before Going Live

- [ ] START.bat completed successfully (all 3 containers "Up")
- [ ] Browser can reach http://192.168.31.4:3000 
- [ ] You're logged in with your credentials
- [ ] API Keys section shows "Connected ✓" for Zerodha
- [ ] You have minimum 1 strategy (max 3)
- [ ] Each strategy "Test Run" passed successfully
- [ ] Each strategy status is "live" (not "active")
- [ ] Laptop has >500 MB free RAM available
- [ ] MONITOR.bat shows CPU <50%, Backend RAM <150MB
- [ ] You understand: Running >3 strategies is risky on this hardware
- [ ] You understand: Market volatility = higher CPU usage = risk of missing ticks

---

## Important Warnings

### ⚠️ YOUR LAPTOP WILL NOT HANDLE:
- More than 3 strategies running simultaneously
- More than 2 strategies on 1-minute timeframes
- Network disconnects (auto-reconnection may fail)
- Market crashes (liquidity dry-ups = API slowness)
- Overnight running (laptop might sleep/restart)

### ⚠️ ZERODHA RATE LIMITS:
- 100 API calls/minute limit
- 5+ strategies @ 30-second tick = ~10 calls/second = 600/minute = RATE LIMIT EXCEEDED
- Solution: Run max 2-3 strategies on different symbols/timeframes

### ⚠️ MONEY MANAGEMENT:
- Set "Max Daily Loss" in each strategy (e.g., -5000 rupees)
- Use small position sizes initially
- Paper trade first until confident
- Never go all-in

---

## Next Steps

1. **Read** the sections above
2. **Create** shortcuts to START.bat, STOP.bat, MONITOR.bat on your desktop
3. **Practice** starting/stopping the system 2-3 times
4. **Paper trade** with 1 strategy for a full trading session
5. **Verify** everything works as expected
6. **Then** go live with real orders (start small position sizes)

---

## Support & Debugging

### If something breaks, check in this order:

1. **Backend logs:** `docker logs quantg-backend | tail -100`
2. **MongoDB status:** `docker exec quantg-mongo mongosh --eval "db.adminCommand('ping')"`
3. **Frontend console:** Open browser (F12) → Console tab → look for errors
4. **Docker stats:** `docker stats --no-stream` → Check memory/CPU
5. **Full restart:** `docker-compose down` then `docker-compose up -d`

### Common error messages:

| Error | Fix |
|-------|-----|
| "Cannot connect to API" | Backend not running, check `docker ps` |
| "MongoDB connection timeout" | MongoDB not running, `docker restart quantg-mongo` |
| "Zerodha API timeout" | Check internet, reduce strategies, wait 5 min |
| "Rate limit exceeded" | Too many strategies, pause one |
| "Order placement failed" | Risk check failed, or insufficient balance |

---

## Summary

**Your system is ready for live trading with 2-3 strategies safely running on your current hardware.**

Start with START.bat each morning, monitor with MONITOR.bat, stop with STOP.bat each evening.

Happy trading! 📈
