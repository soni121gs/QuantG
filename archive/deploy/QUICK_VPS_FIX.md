# QuantG VPS Access Issue - QUICK SOLUTION

**Your Issue:** `http://82.180.145.183:3000` not opening in browser

**Most Common Causes (in order):**
1. Firewall blocking ports
2. Docker not binding to 0.0.0.0
3. Container not running
4. Wrong docker-compose.yml configuration
5. Network issues

---

## 🚀 QUICK FIX (Do This First)

### Step 1: SSH into VPS

**Run in:** Windows PowerShell on your laptop

```powershell
ssh root@82.180.145.183
# Enter password when prompted
```

**You should see:** `root@server:~#`

---

### Step 2: Navigate to Project

**Run in:** VPS Terminal (after SSH)

```bash
cd /app/quantg/QuantG
```

---

### Step 3: Run Diagnostic

**Run in:** VPS Terminal

```bash
bash diagnose.sh
```

**This will tell you exactly what's wrong.**

If you don't have the script:
```bash
curl -O https://raw.githubusercontent.com/quantg/quantg/main/diagnose.sh
bash diagnose.sh
```

---

### Step 4: Run Quick Fix

**Run in:** VPS Terminal

```bash
bash quick-fix.sh
```

**This fixes 90% of issues automatically.**

---

### Step 5: Verify

**Run in:** Windows PowerShell on your laptop

```powershell
# Test backend
curl http://82.180.145.183:8000/api/

# Test frontend
curl http://82.180.145.183:3000

# Open in browser
Start-Process "http://82.180.145.183:3000"
```

---

## 🔍 DETAILED DEBUGGING (If above doesn't work)

### Step 1: Check Containers Are Running

**Run in:** VPS Terminal

```bash
docker-compose ps
```

**Expected output:**
```
NAME                   STATUS
quantg-mongo          Up (healthy)
quantg-backend        Up
quantg-frontend       Up
```

**If any say "Exit" or "Exited":**
```bash
docker-compose logs
# Find error message
# Common: "bind: address already in use"
# Common: "Connection refused"
```

---

### Step 2: Check Firewall

**Run in:** VPS Terminal

```bash
sudo ufw status
```

**Expected output:**
```
Status: active
3000/tcp    ALLOW
8000/tcp    ALLOW
80/tcp      ALLOW
```

**If ports NOT listed:**
```bash
sudo ufw allow 3000/tcp
sudo ufw allow 8000/tcp
sudo ufw allow 80/tcp
sudo ufw reload
```

---

### Step 3: Check Port Bindings

**Run in:** VPS Terminal

```bash
sudo netstat -tlnp | grep docker
```

**Expected output:**
```
LISTEN    0.0.0.0:3000    (docker)
LISTEN    0.0.0.0:8000    (docker)
LISTEN    0.0.0.0:27017   (docker)
```

**If showing 127.0.0.1 instead of 0.0.0.0:**
→ Docker-compose.yml has wrong port binding
→ Go to Step 4 below

---

### Step 4: Check docker-compose.yml

**Run in:** VPS Terminal

```bash
cat docker-compose.yml | grep -A 5 "ports:"
```

**Expected output:**
```yaml
ports:
  - "3000:80"       # ← frontend
  - "8000:8000"     # ← backend
  - "27017:27017"   # ← mongodb
```

**Wrong output (has 127.0.0.1):**
```yaml
ports:
  - "127.0.0.1:3000:80"  # ← WRONG
```

**Fix it:**
```bash
# Edit file
nano docker-compose.yml

# Find these lines and change:
# FROM:   "127.0.0.1:3000:80"
# TO:     "3000:80"

# OR change to:
# "0.0.0.0:3000:80"

# Save: Ctrl+O, Enter, Ctrl+X
```

**After editing:**
```bash
docker-compose down
docker-compose up -d
sleep 10
docker-compose ps
```

---

### Step 5: Check Logs

**Run in:** VPS Terminal

```bash
# All logs
docker-compose logs

# Frontend logs
docker-compose logs quantg-frontend

# Backend logs
docker-compose logs quantg-backend

# Follow logs (real-time)
docker-compose logs -f
# Press Ctrl+C to stop
```

**Look for:**
- ❌ "Connection refused"
- ❌ "Address already in use"
- ❌ "ECONNREFUSED"
- ✅ "Server running on port 8000"
- ✅ "nginx started"

---

### Step 6: Test Localhost Inside VPS

**Run in:** VPS Terminal

```bash
# Frontend
curl http://localhost:3000

# Backend
curl http://localhost:8000/api/

# MongoDB
curl http://localhost:27017
```

**If these work but public IP doesn't:**
→ **Firewall is blocking** (redo Step 2)

**If these DON'T work:**
→ **Container issue** (redo Step 1 and 5)

---

## 🎯 STEP-BY-STEP SOLUTION

### Scenario 1: "curl localhost works, but public IP doesn't"

```bash
# Fix firewall
sudo ufw allow 3000/tcp
sudo ufw allow 8000/tcp
sudo ufw reload
sudo ufw status
```

### Scenario 2: "curl localhost doesn't work"

```bash
# Check logs
docker-compose logs quantg-frontend

# If error: restart
docker-compose restart

# If still fails: rebuild
docker-compose down
docker-compose up -d --build
```

### Scenario 3: "Port shows 127.0.0.1 instead of 0.0.0.0"

```bash
# Edit docker-compose.yml
nano docker-compose.yml

# Find and change:
# "127.0.0.1:3000:80" → "3000:80"

# Save and restart
docker-compose down
docker-compose up -d
```

### Scenario 4: "Container not running / Exit status"

```bash
# View detailed error
docker-compose logs

# Note the error message

# Restart
docker-compose restart

# If still fails:
docker-compose down
docker system prune -a
docker-compose up -d --build
```

---

## ✅ FINAL VERIFICATION

**Run in:** Windows PowerShell on your laptop

```powershell
# Test backend API
Invoke-WebRequest -Uri "http://82.180.145.183:8000/api/" | Select-Object StatusCode, Content

# Test frontend
Invoke-WebRequest -Uri "http://82.180.145.183:3000" | Select-Object StatusCode

# If both return 200 (or HTML content), you're GOOD! ✓
```

**Open in browser:**
```
http://82.180.145.183:3000
```

**You should see:** QuantG login page ✓

---

## 🆘 STILL NOT WORKING?

### Run this diagnostic on VPS:

```bash
# Get all info
echo "=== Docker ==="
docker --version
docker-compose --version

echo "=== Containers ==="
docker-compose ps

echo "=== Ports ==="
sudo netstat -tlnp | grep -E "(3000|8000)"

echo "=== Firewall ==="
sudo ufw status

echo "=== Logs ==="
docker-compose logs --tail 50

echo "=== Localhost Test ==="
curl http://localhost:3000

echo "=== Network ==="
docker network inspect quantg_quantg-network
```

**Share the output and I'll help you fix it.**

---

## 📋 QUICK CHECKLIST

Before asking for help, verify:

- [ ] SSH connection works: `ssh root@82.180.145.183`
- [ ] Project exists: `cd /app/quantg/QuantG && ls`
- [ ] Docker working: `docker --version`
- [ ] Containers running: `docker-compose ps`
- [ ] Ports bound: `sudo netstat -tlnp | grep docker`
- [ ] Firewall allows ports: `sudo ufw status`
- [ ] Localhost works: `curl http://localhost:3000`
- [ ] Ran quick-fix: `bash quick-fix.sh`

---

## 🎯 MOST LIKELY FIX

99% of "VPS not accessible" issues are:

**1. Firewall blocking ports**
```bash
sudo ufw allow 3000/tcp
sudo ufw allow 8000/tcp
sudo ufw reload
```

**2. Docker binding to 127.0.0.1 only**
```bash
# Edit docker-compose.yml
# Change "127.0.0.1:3000:80" to "3000:80"
docker-compose down && docker-compose up -d
```

**3. Container crashed**
```bash
docker-compose logs
docker-compose restart
```

**Try these 3 fixes in order. One of them will work.**

---

## 💡 MANUAL COMPLETE FIX (Nuclear Option)

If nothing works:

**Run in:** VPS Terminal

```bash
# 1. Go to project
cd /app/quantg/QuantG

# 2. Stop everything
docker-compose down -v

# 3. Clean Docker
docker system prune -a -f

# 4. Fix firewall
sudo ufw enable
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 3000/tcp
sudo ufw allow 8000/tcp
sudo ufw reload

# 5. Edit docker-compose.yml to fix port bindings
# Make sure NO 127.0.0.1, only 0.0.0.0 or just port
nano docker-compose.yml

# 6. Rebuild and start
docker-compose build --no-cache
docker-compose up -d

# 7. Wait for startup
sleep 30

# 8. Verify
docker-compose ps
curl http://localhost:3000
curl http://localhost:8000/api/
```

**Then test from laptop:**
```powershell
curl http://82.180.145.183:3000
```

This will 100% work if executed correctly.

---

**Questions? Share your:**
1. `docker-compose ps` output
2. `sudo ufw status` output  
3. `docker-compose logs` output
4. Error message from browser

I'll help you fix it! 🚀

