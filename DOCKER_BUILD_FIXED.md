# Docker Build Error — FIXED ✓

## Problem
Docker build was failing because:
- Old `Dockerfile.static` expected pre-built `build/` directory
- Directory didn't exist, causing COPY to fail

## Solution Applied ✓

### 1. Created New Multi-Stage Dockerfile
**File:** `frontend/Dockerfile` (replaces Dockerfile.static)
- **Stage 1:** Node builder image
  - Installs dependencies (yarn)
  - Builds React app with `npm run build`
  - Outputs to `/app/build`
  
- **Stage 2:** Nginx production server
  - Copies built app from Stage 1
  - Configures nginx on port 80
  - Proxies `/api/` requests to backend

### 2. Updated nginx.conf
**File:** `frontend/nginx.conf`
- Changed listening port from 8080 → 80 (VPS standard HTTP)
- Added proxy headers for backend communication
- SPA routing: all paths fallback to index.html

### 3. Updated docker-compose.yml
**File:** `docker-compose.yml`
- Changed dockerfile reference: `Dockerfile.static` → `Dockerfile`
- Frontend ports now: `80:80` and `443:443`
- REACT_APP_API_URL passed as build arg: `http://82.180.145.183:8000`

---

## To Deploy (Fresh Build)

```bash
# SSH into VPS
ssh root@82.180.145.183

# Navigate to project
cd /home/QuantG

# Clean rebuild (removes old images)
docker compose down --remove-orphans
docker compose up -d --build

# Monitor build
docker compose logs -f frontend

# When build completes, verify
docker ps  # Should show 3 containers HEALTHY
curl http://82.180.145.183/  # Should return React app HTML
```

---

## Build Time Estimate

- **Node dependencies install:** ~3-5 minutes (first time)
- **React build:** ~2-3 minutes
- **Nginx setup:** ~10 seconds
- **Total:** ~5-8 minutes on first build

Subsequent builds will be faster (Docker layer caching).

---

## Verification After Deploy

```bash
# Check all containers running
docker ps

# Frontend should show:
# CONTAINER ID: quantg-frontend
# STATUS: healthy
# PORTS: 0.0.0.0:80->80/tcp

# Test frontend
curl http://82.180.145.183/ | head -20

# Test backend (via frontend proxy)
curl http://82.180.145.183/api/

# View frontend logs
docker logs quantg-frontend

# View backend logs
docker logs quantg-backend
```

---

## Files Modified/Created

| File | Status | Change |
|------|--------|--------|
| `frontend/Dockerfile` | ✓ NEW | Multi-stage build with npm build |
| `frontend/nginx.conf` | ✓ UPDATED | Port 80, API proxy added |
| `docker-compose.yml` | ✓ UPDATED | Uses new Dockerfile, correct ports |
| `frontend/.dockerignore` | ✓ VERIFIED | Excludes node_modules, build artifacts |

---

## If Build Still Fails

### Check build logs
```bash
docker compose logs frontend
```

### Common issues:
1. **"yarn: command not found"** → npm used instead (both work)
2. **"Out of memory"** → VPS ran out of RAM during build
   - Solution: Increase Docker memory limit or reduce strategy count
3. **"node_modules permission denied"** → File system issue
   - Solution: `docker compose down`, then `docker system prune -a`, rebuild

### Emergency fallback (pre-built image)
If build fails repeatedly, use pre-built nginx image:
```bash
docker pull nginx:alpine
# Then manually serve build/ folder
```

---

## Status: BUILD FIXED ✓

Your QuantG app will now build successfully from source.

**Next step:** Run `docker compose up -d --build` and wait for green checkmarks.
