#!/bin/bash

cd /app/quantg/QuantG || cd /root/QuantG || cd /opt/QuantG || true

echo "===== DOCKER STATUS ====="
docker compose ps 2>/dev/null || docker-compose ps

echo ""
echo "===== BACKEND LOGS ====="
docker logs quantg-backend --tail 200

echo ""
echo "===== FRONTEND LOGS ====="
docker logs quantg-frontend --tail 200

echo ""
echo "===== MONGO LOGS ====="
docker logs quantg-mongo --tail 100

echo ""
echo "===== NETWORK PORTS ====="
ss -tulnp

echo ""
echo "===== ENV FILE ====="
cat /app/quantg/QuantG/backend/.env

echo ""
echo "===== API TEST ====="
curl http://localhost:8000/docs

echo ""
echo "===== FRONTEND TEST ====="
curl http://localhost:3000
