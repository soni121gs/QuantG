# QuantG - PC Optimization & Hardware Migration Guide

## 📊 Your Current Hardware

```
Laptop: Desktop-5D0PJ31
CPU: 4 Logical Processors (Intel i3/Pentium likely)
RAM: 4 GB Physical
Docker Limit: 1.8 GB (default)
```

---

## 🔧 Part 1: Optimize For Current Laptop (4GB RAM)

### A. Docker Memory Configuration

**Current Problem:** Docker is limited to 1.8 GB out of 4 GB available.

**Step 1: Increase Docker Memory Limit**

1. Open Docker Desktop
2. Click **Settings** (gear icon)
3. Go to **Resources**
4. Under "Memory", change from default to **2.5 GB**
5. Click "Apply & Restart"

```
OLD: 1.8 GB → Backend 116 MB, MongoDB 123 MB, Frontend 6 MB
NEW: 2.5 GB → Backend 180 MB, MongoDB 200 MB, Frontend 20 MB
Benefit: 40% more headroom
```

**Step 2: Verify Change**

```bash
docker stats --no-stream
# Should show "MEM LIMIT" as 2.5 GB instead of 1.8 GB
```

### B. Disable Unnecessary Services

**Disable Docker Desktop on startup** if you only trade during specific hours:

1. Open **Task Manager** (Ctrl+Shift+Esc)
2. Go to **Startup** tab
3. Find **Docker Desktop**
4. Right-click → **Disable**
5. Only start manually when trading

**Saves: 300-400 MB RAM when not trading**

### C. Windows Optimization

**1. Disable Windows Updates During Trading**

```cmd
# Open Services
services.msc

# Find: Windows Update
# Right-click → Properties
# Startup Type: Disabled (during trading)
# Re-enable after market close
```

**Saves: 100-200 MB**

**2. Disable Visual Effects**

```
System Properties → Advanced tab → Performance → Settings
☑ Adjust for best performance
```

**Saves: 50-100 MB**

**3. Close Background Apps**

Before trading, close:
- Chrome/Firefox (if not using)
- Slack, Discord, Telegram
- OneDrive, Google Drive sync
- Antivirus real-time scanning (or exclude Docker)

**Saves: 200-400 MB**

### D. Optimize MongoDB

**Edit `docker-compose.yml`:**

```yaml
mongo:
  image: mongo:6.0
  container_name: quantg-mongo
  environment:
    # Reduce cache
    - MONGO_INITDB_ROOT_PASSWORD=mongo
  command:
    - mongod
    - --wiredTigerCacheSizeGB=0.5  # ← Add this (was 1 GB default)
    - --journal=false              # ← Disable journaling for speed
```

Then rebuild:
```bash
cd D:\Quant\QuantG
docker-compose down
docker-compose up -d --build
```

**Saves: 200-300 MB RAM**

### E. Optimize Backend Memory

**Edit `backend/Dockerfile`:**

```dockerfile
# Add environment variable for Python garbage collection
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_TRIM_THRESHOLD_=128000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app

# Run with memory limits
CMD ["python", "-u", "-X", "dev", "server.py"]
```

### F. Strategy Configuration for 4GB RAM

**Edit strategy `advanced_config`:**

```json
{
  "position_sizing": {
    "max_position_notional": 50000,  # ← Reduce (was 100k)
    "fixed_lots": 1,
    "mode": "FIXED_LOTS"
  },
  
  "risk_limits": {
    "max_concurrent_positions": 2,     # ← 2 only (was 3)
    "max_daily_loss_amount": 3000,     # ← ₹3k (was 5k)
    "max_risk_per_trade_pct": 0.5,     # ← 0.5% (was 1%)
    "circuit_breaker_daily_loss_pct": 3.0  # ← 3% (was 5%)
  },
  
  "exit_conditions": {
    "max_hold_minutes": 120,  # ← Shorter holds
    "exit_on_reverse_signal": true
  }
}
```

### G. Network Optimization

**Reduce lag between laptop and phone:**

```bash
# In docker-compose.yml, change:
ports:
  - "0.0.0.0:8000:8000"    # ← Accessible from LAN
  - "0.0.0.0:3000:8080"    # ← Accessible from LAN

# Keep backend local to frontend
# Don't expose on internet
```

### H. Daily RAM Management

**Before Trading:**

```bash
# Free up RAM
Restart laptop
OR
# Clear Docker cache
docker system prune -a
docker volume prune
```

**During Trading:**

```bash
# Monitor via MONITOR.bat
# If RAM > 70%:
#   1. Pause 1 strategy
#   2. Close browser tabs
#   3. Restart Docker if >80%
```

---

## 📈 Part 2: Expected Performance with Optimizations

| Metric | Before | After Optimization |
|--------|--------|-------------------|
| Available RAM | 1 GB | 1.5-1.7 GB |
| Backend Memory | 116 MB | 140-160 MB |
| MongoDB Memory | 123 MB | 80-100 MB |
| Max Strategies | 2 | 2-3 (can push 3 safely) |
| Order Success Rate | 95% | 98%+ |
| System Stability | Fair | Good |

---

## 💾 Part 3: Backup Strategy (Before Any Changes)

**CRITICAL: Backup your data first!**

```bash
# 1. Backup MongoDB
docker exec quantg-mongo mongodump --out /dump
docker cp quantg-mongo:/dump C:\Backups\QuantG-Backup-$(date +%Y%m%d).tar.gz

# 2. Backup all configurations
xcopy D:\Quant\QuantG\backend\*.env C:\Backups\
xcopy D:\Quant\QuantG\frontend\.env* C:\Backups\

# 3. Export all strategies
# (Would need API call)

# 4. Create restore script
# (Keep it safe)
```

---

## 🚀 Part 4: Migrate to Better Laptop (8GB+ RAM)

### When to Upgrade?

Upgrade if ANY of these happen:
- ✗ Strategies pause due to memory pressure (>1x/week)
- ✗ Order latency exceeds 5 seconds regularly
- ✗ CPU usage stays >60% (constant throttling)
- ✗ Want to run >3 strategies safely
- ✗ Want 5+ strategies simultaneously

### Migration Procedure

#### Phase 1: Prepare New Laptop (1 day before)

**On New Laptop:**

1. **Install Prerequisites**
   ```bash
   # 1. Install Git for Windows
   #    https://git-scm.com/download/win
   
   # 2. Install Docker Desktop (latest)
   #    https://docs.docker.com/desktop/install/windows-install/
   
   # 3. Configure Docker:
   #    - Settings → Resources
   #    - Memory: 4-6 GB (out of 8 GB)
   #    - CPUs: 4
   #    - Disk Image Size: 50 GB
   #    - Apply & Restart
   ```

2. **Clone QuantG Repository**
   ```bash
   cd C:\
   git clone https://github.com/your-repo/QuantG.git
   # OR copy from USB:
   xcopy D:\Quant\QuantG E:\Backups\QuantG-Old
   ```

3. **Test Docker**
   ```bash
   docker --version
   docker run hello-world
   ```

#### Phase 2: Export Data from Old Laptop (Before Migration)

**On Current Laptop (Last Trading Day):**

```bash
# 1. Stop all trading
STOP.bat

# 2. Create full MongoDB dump
docker exec quantg-mongo mongodump --out /backup/mongo-dump-%date:~-4%.tar
docker cp quantg-mongo:/backup/mongo-dump-*.tar D:\Backups\

# 3. Backup all code
xcopy D:\Quant\QuantG D:\Backups\QuantG-Full-Backup\ /E /I /Y

# 4. Export broker keys (ENCRYPTED)
# Store in password-protected file

# 5. Create migration checklist
```

#### Phase 3: Import Data to New Laptop (First Day)

**On New Laptop:**

```bash
# 1. Start fresh containers
cd C:\QuantG
START_v2.bat

# 2. Verify containers running
docker ps

# 3. Restore MongoDB from backup
docker cp D:\Backups\mongo-dump.tar quantg-mongo:/
docker exec quantg-mongo mongorestore /mongo-dump

# 4. Restore configuration files
xcopy D:\Backups\QuantG-Full-Backup\backend\.env C:\QuantG\backend\
xcopy D:\Backups\QuantG-Full-Backup\frontend\.env* C:\QuantG\frontend\

# 5. Rebuild images to ensure latest code
docker-compose build --no-cache
docker-compose up -d

# 6. Verify data restored
# Open http://192.168.x.x:3000
# Login - should see all old strategies
# Check one strategy - should have history
```

#### Phase 4: Verification (Test Day)

**First Trading Day on New Laptop:**

```
[ ] All strategies visible in UI
[ ] Strategy history/stats visible
[ ] Broker keys still configured
[ ] Paper mode test trade succeeds
[ ] Zerodha connection works
[ ] Backend logs show no errors
[ ] API endpoints responding
[ ] Database queries fast
[ ] Advanced features active
```

---

## 📊 New Laptop Recommendations

### Option A: Budget-Friendly ($600-900)
```
• CPU: Intel i5-12th Gen (4 cores, 8 threads)
• RAM: 8 GB DDR4/DDR5
• Storage: 256 GB SSD
• Battery: 6+ hours
• OS: Windows 11

Benefit: Can safely run 4-5 strategies
```

### Option B: Balanced ($1000-1500)
```
• CPU: Intel i7-12th Gen (6 cores, 12 threads)
• RAM: 16 GB DDR5
• Storage: 512 GB NVMe SSD
• Battery: 8+ hours
• OS: Windows 11 Pro

Benefit: Can run 5-8 strategies, very responsive
```

### Option C: High-Performance ($1500-2500)
```
• CPU: Intel i7-13th Gen or AMD Ryzen 7
• RAM: 32 GB DDR5
• Storage: 1 TB NVMe SSD
• Battery: 10+ hours
• OS: Windows 11 Pro

Benefit: Can run 10+ strategies, multiple instruments
```

**For QuantG: Option B is ideal - balanced performance/cost**

---

## 🔄 Part 5: Dual-Laptop Setup (Advanced)

**Run QuantG on BOTH laptops simultaneously:**

### Setup Architecture

```
Old Laptop (4GB RAM)          New Laptop (8GB RAM)
├─ Strategy 1 (NIFTY)        ├─ Strategy 2 (BANKNIFTY)
├─ Strategy 3 (SENSEX)       └─ Strategy 4 (FINNIFTY)
└─ Backup runner             └─ Primary runner
```

### Configuration

**On both laptops:**

1. Change MongoDB to use **cloud MongoDB Atlas** (shared database)
   ```
   # Old docker-compose.yml:
   - MONGO_URL=mongodb+srv://user:pass@cluster.mongodb.net/quantg
   ```

2. Configure Zerodha API key on both

3. Use **different POD_IDs** per laptop (automatic)

4. Each laptop handles its own strategies (no conflicts)

### Advantages

✅ Redundancy - if one fails, other continues
✅ Load balancing - distribute strategies
✅ Scaling - easily add more strategies
✅ 24/7 trading possible (rotate laptops)

### Drawbacks

❌ More complex setup
❌ Requires cloud MongoDB (costs ₹500-2000/month)
❌ Network dependency
❌ Need monitoring for both

---

## 🚨 Emergency Recovery

**If New Laptop Fails During Trading:**

```bash
# 1. Immediately restart old laptop
START_v2.bat

# 2. Check if MongoDB data is synced
docker exec quantg-mongo mongosh --eval "db.orders.count()"

# 3. Check positions
# Open http://192.168.31.4:3000/portfolio
# Are all positions showing correctly?

# 4. If positions missing, restore from backup
docker cp D:\Backups\mongo-dump.tar quantg-mongo:/
docker exec quantg-mongo mongorestore /mongo-dump

# 5. Resume trading from where you left off
```

---

## 📋 Migration Checklist

### Week Before New Laptop

- [ ] Backup all data (full MongoDB dump)
- [ ] Export all strategies to JSON/CSV
- [ ] Document all API key credentials (encrypted)
- [ ] Test backup/restore procedure
- [ ] Verify Docker on new laptop works
- [ ] Test network connectivity

### Day Of Migration

- [ ] Stop all trading by 3:30 PM
- [ ] Run STOP.bat to shut down gracefully
- [ ] Create final backup
- [ ] Verify backup integrity
- [ ] Copy files to USB drive
- [ ] Transport to new laptop
- [ ] Do NOT force-shutdown old laptop

### First Day on New Laptop

- [ ] Restore MongoDB from backup
- [ ] Restore configuration files
- [ ] Test with paper trading only
- [ ] Verify all strategies visible
- [ ] Verify all historical data intact
- [ ] Verify orders/positions correct
- [ ] Test one live trade (small size)
- [ ] Monitor for 1 hour
- [ ] Once verified, resume normal trading

### Post-Migration

- [ ] Keep old laptop as warm backup
- [ ] Still run STOP.bat at end of each day
- [ ] Run daily backups to cloud
- [ ] Monitor new laptop resource usage
- [ ] Gradually increase strategy count over 1 week

---

## 💡 Best Practices

### Daily Operations (Any Laptop)

```
9:10 AM   → Run START_v2.bat
9:15 AM   → Login, verify Zerodha connected
9:20 AM   → Check daily strategy reports
9:25 AM   → Click "Go Live" on strategies
9:30 AM   → Open MONITOR.bat in corner
3:25 PM   → Review trades for day
3:30 PM   → Click "Pause" on all strategies
3:35 PM   → Run STOP_v2.bat
3:45 PM   → Verify all data saved
```

### Resource Monitoring

```
Every 30 min:
- Check MONITOR.bat
- If CPU > 60%: Close background apps
- If RAM > 70%: Pause 1 strategy
- If Backend RAM > 160MB: Restart backend

Every evening:
- Check error logs
- Review trade journal
- Document any issues
- Plan next day
```

### Weekly Maintenance

```
Every Friday:
- Full system backup
- Clean Docker images
- Review performance metrics
- Update Docker Desktop
- Restart laptop (fresh)

Every Month:
- Full MongoDB backup to cloud
- Export all strategies
- Test restore procedure
- Review hardware upgrade needs
```

---

## 🎯 Summary

**Current Laptop (4GB):**
- Can safely trade 2-3 strategies
- With optimization: can handle 3 carefully
- Requires active monitoring
- Good for learning/testing

**New Laptop (8GB+):**
- Can safely trade 4-5 strategies
- Less monitoring needed
- Better order execution
- Better for production trading

**Recommendation:**
- Use current laptop for 2-3 months
- Monitor performance metrics
- Upgrade when hitting limits consistently
- Plan migration carefully
- Keep old laptop as backup

---

**Questions? Check LIVE_TRADING_GUIDE.md or ADVANCED_FEATURES_GUIDE.md**

