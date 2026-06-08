# QuantG

NSE/BSE options algo-trading platform. FastAPI backend, React frontend, MongoDB, Upstox broker.

**AI agents: read [CLAUDE.md](CLAUDE.md) first.**

## Quick Start (Docker)

```powershell
docker compose build
docker compose up -d
curl http://127.0.0.1:8000/api/health
```

## Local Dev (no Docker)

```powershell
# Backend
cd backend && uvicorn server:app --host 0.0.0.0 --port 8000 --reload

# Frontend
cd frontend && npm install && npm start
```

## Production

See [DEPLOY.md](DEPLOY.md) for VPS deployment steps.
