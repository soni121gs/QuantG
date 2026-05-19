# 🚀 ONE-MINUTE DEPLOYMENT REFERENCE

## The Fix
- **Problem:** Docker build failing (missing `build/` directory)
- **Solution:** Created multi-stage Dockerfile that builds React inside Docker
- **Status:** ✅ FIXED

---

## Deploy Now (Copy/Paste)

### On Your VPS (SSH)
```bash
ssh root@82.180.145.183
cd /home/QuantG
docker-compose down --remove-orphans
docker-compose up -d --build
```

### On Windows (PowerShell)
```powershell
cd D:\Quant\QuantG
.\deploy.ps1
```

### On Linux/Mac (Bash)
```bash
cd /home/QuantG
bash deploy.sh
```

---

## Wait For
⏱️ **5-10 minutes** (first build only)

Monitor:
```bash
docker-compose logs -f
```

Expected when done:
```
quantg-mongo       healthy
quantg-backend     running
quantg-frontend    healthy
```

---

## Then Open
```
http://82.180.145.183:80
```

---

## First-Time Setup (10 minutes)
1. **Register** new account
2. **Broker Keys** → Save API Key + Secret
3. **Broker Keys** → Click "Connect to Zerodha"
4. **Kite Login** → Grant permissions → Auto-redirects
5. **Strategies** → Click "Seed Defaults" → 10 strategies loaded
6. **Select strategy** → "Test Run" → Verify works
7. **Click "Go Live"** → Status changes to LIVE + SCANNING

---

## You're Live!
Monitor dashboard in real-time. Strategies will fire orders automatically.

---

## Quick Commands

| Command | Purpose |
|---------|---------|
| `docker ps` | View all containers |
| `docker-compose logs -f backend` | Watch backend logs |
| `docker-compose logs -f frontend` | Watch frontend logs |
| `docker-compose restart backend` | Restart backend |
| `docker-compose down` | Stop everything |
| `curl http://82.180.145.183:8000/api/` | Test backend |
| `curl http://82.180.145.183/` | Test frontend |

---

## Zerodha OAuth Setup (Kite Developer App)

Set **Redirect URL** to:
```
http://82.180.145.183:8000/api/zerodha/exchange
```

Then get your:
- **API Key**
- **API Secret**

Paste into QuantG **Broker Keys** page.

---

## Performance Limits
- CPU: Keep < 50%
- RAM: ~300 MB total
- Strategies: 2-3 max concurrent
- Ticks: 30 seconds minimum

Monitor:
```bash
docker stats
```

---

## If It Breaks

### Backend not responding
```bash
docker logs quantg-backend | tail -20
docker-compose restart backend
```

### Frontend showing blank
```bash
docker logs quantg-frontend | tail -20
curl http://82.180.145.183/ | head -20
```

### API calls failing
Verify API URL is correct:
```bash
docker inspect quantg-frontend | grep REACT_APP_API_URL
```

### Out of memory
Build failed due to insufficient RAM:
```bash
docker system prune -a
docker-compose up -d --build
```

---

## Status: READY FOR DEPLOYMENT ✅

**All files configured. Deploy with confidence.**

🎯 **Target:** Live trading tomorrow morning
⏰ **Time:** 5-10 minutes to deploy + 10 minutes to setup
📊 **Result:** Fully automated trading terminal

Let's go! 🚀
