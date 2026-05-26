# QuantG

Existing QuantG app: FastAPI backend, React frontend, MongoDB ledger, and
Upstox-only trading flow.

## Local Runbook

### Local backend

```powershell
cd backend
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

### Frontend

```powershell
cd frontend
npm install
npm start
```

### Docker

Start Docker Desktop first, then:

```powershell
docker compose build
docker compose up -d
```

### Health check

```powershell
curl http://127.0.0.1:8000/api/health
```

### Integration test preflights

The paper trade tests expect the backend to already be running on
`127.0.0.1:8000`. If it is not running, tests skip with:

```text
Backend not running. Start backend before paper trade test.
```

Docker checks skip with:

```text
Docker daemon not running. Start Docker Desktop or run on VPS.
```
