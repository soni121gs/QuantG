# QuantG VPS Debugging & Fix Guide - Step by Step

**Your Setup:**
- Windows Laptop: Local Docker running ✓
- Contabo VPS: Ubuntu 24.04, Docker installed ✓
- Project: `/app/quantg/QuantG`
- Issue: `http://82.180.145.183:3000` not opening

**We'll fix this step-by-step.**

---

## 🔧 STEP 1: SSH Into Your VPS

### Where to Run: Windows PowerShell

```powershell
# Open PowerShell on your laptop
# Connect to your VPS (replace with your actual credentials)

ssh root@82.180.145.183

# When prompted, enter your Contabo password
# You should see: root@server:~#
```

**You are now INSIDE your VPS terminal.**

---

## ✅ STEP 2: Verify You're in the Right Directory

### Where to Run: VPS Terminal

```bash
# Check current location
pwd

# Output should show: /root or /home/username
# Navigate to project
cd /app/quantg/QuantG

# Verify files exist
ls -la

# You should see: docker-compose.yml, .env, frontend/, backend/, etc.
```

---

## 🐳 STEP 3: Check if Docker & Docker Compose Work

### Where to Run: VPS Terminal

```bash
# Check Docker version
docker --version

# Check Docker Compose version
docker-compose --version

# Check Docker service status
systemctl status docker

# Output should show: active (running)
# If not: sudo systemctl start docker
```

**If either fails, Docker isn't installed correctly. Let me know and we'll reinstall.**

---

## 📦 STEP 4: Check Container Status

### Where to Run: VPS Terminal

```bash
# Navigate to project directory
cd /app/quantg/QuantG

# Check all containers
docker-compose ps

# Expected output:
# NAME                STATUS              PORTS
# quantg-mongo        Up (healthy)        27017->27017/tcp
# quantg-backend      Up                  8000->8000/tcp
# quantg-frontend     Up                  80->80/tcp (or 3000->3000/tcp)
```

**What to look for:**
- ✅ All containers say "Up"
- ✅ Ports show proper mappings
- ✅ No "Exit" or "Exited" status

**If any container is not "Up":**
```bash
# View detailed logs
docker-compose logs

# Restart containers
docker-compose restart

# Wait 10 seconds
docker-compose ps
```

---

## 🔍 STEP 5: View Container Logs

### Where to Run: VPS Terminal

```bash
# View logs for all containers
docker-compose logs

# View logs for specific service
docker-compose logs quantg-frontend

# View logs for backend
docker-compose logs quantg-backend

# View logs for MongoDB
docker-compose logs quantg-mongo

# View logs and follow (real-time)
docker-compose logs -f

# Press Ctrl+C to stop following
```

**What to look for:**
- ✅ "Server running on port 8000" (backend)
- ✅ "nginx started" or "serving on port 80/3000" (frontend)
- ✅ No "Error", "ConnectionRefused", or "Exit" messages

**If you see errors, note them down and we'll fix them.**

---

## 🌐 STEP 6: Check Docker Network Configuration

### Where to Run: VPS Terminal

```bash
# List all Docker networks
docker network ls

# Inspect the quantg network
docker network inspect quantg_quantg-network

# Expected output shows:
# - Container names and IPs
# - Network driver (should be "bridge")
# - Connected containers (frontend, backend, mongo)
```

**If network looks wrong:**
```bash
# Recreate network
docker-compose down
docker-compose up -d
```

---

## 🔌 STEP 7: Test Localhost Access Inside VPS

### Where to Run: VPS Terminal

```bash
# Test if frontend is accessible on localhost
curl http://localhost:3000

# Expected output: HTML content or "Welcome" message
# If error: "Connection refused" or "Failed to connect"

# Test if backend is accessible
curl http://localhost:8000/api/

# Expected output: {"status":"ok",...} JSON response
# If error: "Connection refused"

# Test if MongoDB is running
curl http://localhost:27017

# Expected output: curl error (that's OK, just means MongoDB is there)
# If completely fails: MongoDB not running
```

**If localhost works but public IP doesn't:**
→ It's a **firewall issue** (go to Step 9)

**If localhost doesn't work:**
→ Container isn't listening properly (go to Step 8)

---

## 🔥 STEP 8: Check UFW Firewall Rules

### Where to Run: VPS Terminal

```bash
# Check firewall status
sudo ufw status

# Expected output: Status: active
# Lists all allowed ports

# If not active, enable it
sudo ufw enable

# Allow SSH (IMPORTANT! Don't lock yourself out)
sudo ufw allow 22/tcp

# Allow HTTP
sudo ufw allow 80/tcp

# Allow HTTPS
sudo ufw allow 443/tcp

# Allow custom ports
sudo ufw allow 3000/tcp
sudo ufw allow 8000/tcp
sudo ufw allow 27017/tcp

# Check status again
sudo ufw status

# You should see all ports listed as "allow"
```

**Screenshot of correct firewall:**
```
Status: active

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW       Anywhere
80/tcp                     ALLOW       Anywhere
443/tcp                     ALLOW       Anywhere
3000/tcp                   ALLOW       Anywhere
8000/tcp                   ALLOW       Anywhere
27017/tcp                  ALLOW       Anywhere
```

---

## 🐳 STEP 9: Check Docker Port Bindings

### Where to Run: VPS Terminal

```bash
# Check what ports Docker is actually listening on
sudo netstat -tlnp | grep docker

# OR (if netstat not available)
sudo ss -tlnp | grep docker

# Expected output:
# LISTEN ... 0.0.0.0:3000
# LISTEN ... 0.0.0.0:8000
# LISTEN ... 127.0.0.1:27017

# Test with curl on specific port
curl -v http://0.0.0.0:3000

# Check if service listening on all interfaces
ss -tlnp

# Look for lines with 3000 and 8000 ports
```

---

## 🔧 STEP 10: Fix docker-compose.yml Configuration

### Where to Run: VPS Terminal

```bash
# Navigate to project
cd /app/quantg/QuantG

# Edit docker-compose.yml
nano docker-compose.yml

# Look for these sections:
```

**You should see (find these lines):**

```yaml
services:
  frontend:
    ports:
      - "3000:80"    # ← Should be like this
    # OR
      - "0.0.0.0:3000:80"  # ← Or like this

  backend:
    ports:
      - "8000:8000"   # ← Should be like this
    # OR
      - "0.0.0.0:8000:8000"  # ← Or like this
```

**If ports are wrong:**
```yaml
# Change FROM:
ports:
  - "127.0.0.1:3000:80"   # ← Only localhost

# Change TO:
ports:
  - "0.0.0.0:3000:80"     # ← All interfaces
```

**How to edit in nano:**
```
1. Find the line (use Ctrl+W to search)
2. Delete wrong text
3. Type correct text
4. Press Ctrl+O (save)
5. Press Enter (confirm)
6. Press Ctrl+X (exit)
```

**After editing:**
```bash
# Rebuild containers
docker-compose down
docker-compose up -d --build

# Wait 10 seconds
docker-compose ps
```

---

## 🌍 STEP 11: Test From Your Laptop

### Where to Run: Windows PowerShell

```powershell
# Open PowerShell on your LAPTOP (not VPS)

# Test backend endpoint
curl http://82.180.145.183:8000/api/

# Expected: JSON response with {"status":"ok",...}

# Test frontend (this will download HTML)
curl http://82.180.145.183:3000

# Expected: HTML content starts downloading
# If takes too long, Ctrl+C to cancel
```

**Test in browser:**
```
Open in browser:
http://82.180.145.183:3000

If it works: You should see QuantG login page ✓
If it doesn't: Check output of curl command above
```

---

## 🆘 STEP 12: Common Fixes

### Issue: "Connection refused" on localhost

```bash
# Check if container is actually running
docker-compose ps

# If not running:
docker-compose logs

# Restart
docker-compose restart
```

### Issue: Firewall blocking

```bash
# Allow port
sudo ufw allow 3000/tcp

# Reload firewall
sudo ufw reload

# Check status
sudo ufw status
```

### Issue: Docker not binding to 0.0.0.0

```bash
# Edit docker-compose.yml
nano docker-compose.yml

# Change ports from 127.0.0.1 to 0.0.0.0
# Save and restart

docker-compose down
docker-compose up -d
```

### Issue: MongoDB connection error

```bash
# Check MongoDB logs
docker-compose logs quantg-mongo

# Restart MongoDB
docker-compose restart quantg-mongo

# Test connection
docker-compose exec quantg-mongo mongosh --eval "db.adminCommand('ping')"
```

### Issue: Environment variables wrong

```bash
# Check .env file
cat .env

# Make sure REACT_APP_BACKEND_URL is correct:
# Should be: REACT_APP_BACKEND_URL=http://82.180.145.183:8000
# NOT: http://localhost:8000
# NOT: http://backend:8000

# Edit if needed
nano .env

# After editing:
docker-compose down
docker-compose up -d --build
```

---

## 📋 COMPLETE DEBUGGING CHECKLIST

### Run these in order on VPS:

```bash
# 1. SSH into VPS
ssh root@82.180.145.183

# 2. Go to project
cd /app/quantg/QuantG

# 3. Check containers
docker-compose ps

# 4. Check logs
docker-compose logs

# 5. Test localhost
curl http://localhost:3000
curl http://localhost:8000/api/

# 6. Check firewall
sudo ufw status

# 7. Check ports listening
sudo netstat -tlnp | grep docker

# 8. Check docker-compose.yml
cat docker-compose.yml | grep -A 2 "ports:"

# 9. Restart if needed
docker-compose restart
```

### If everything above works but URL still doesn't load:

```bash
# Full reset
docker-compose down
docker system prune -a
docker-compose up -d

# Wait 30 seconds
docker-compose ps

# Test from laptop
# Open: http://82.180.145.183:3000
```

---

## 🎯 EXPECTED SUCCESS

When everything works:

**From your laptop in PowerShell:**
```powershell
curl http://82.180.145.183:3000
# Returns: HTML content (QuantG page)

curl http://82.180.145.183:8000/api/
# Returns: {"status":"ok","service":"QuantG API"}
```

**In browser:**
```
http://82.180.145.183:3000
# Shows: QuantG login page with logo, login form
# Can login with your account
```

**From VPS:**
```bash
curl http://localhost:3000
# Returns: HTML (frontend working)

curl http://localhost:8000/api/
# Returns: JSON (backend working)
```

---

## 🆘 QUICK SOS REFERENCE

If stuck:

1. **Container not starting?**
   ```bash
   docker-compose logs
   ```

2. **Port not accessible?**
   ```bash
   sudo ufw allow 3000/tcp
   sudo ufw reload
   ```

3. **Can't access from laptop?**
   ```bash
   curl http://localhost:3000  # Test on VPS first
   ```

4. **Firewall blocking?**
   ```bash
   sudo ufw status
   # Check if 3000/tcp and 8000/tcp listed
   ```

5. **Docker-compose wrong?**
   ```bash
   nano docker-compose.yml
   # Check ports: should be 0.0.0.0:port
   ```

---

## ✅ DONE!

Once you can access `http://82.180.145.183:3000` from your laptop browser:
- Your QuantG app is **live on the internet** ✓
- You can access from **any device, anywhere** ✓
- Your trading platform is **24/7 online** ✓

**Next steps:**
1. Login to your account
2. Add Zerodha API keys
3. Create strategies
4. Go LIVE! 🚀

