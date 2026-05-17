Run the app locally with Docker Compose (builds images and runs services):

```bash
docker compose up --build -d
```

Services:
- MongoDB: port 27017
- Backend (FastAPI/uvicorn): port 8000
- Frontend (React served by nginx): port 3000 -> nginx:8080

Environment:
- Edit `backend/.env` to configure `MONGO_URL`, `DB_NAME`, `JWT_SECRET`, etc.

To stop and remove containers:

```bash
docker compose down
```

