#!/bin/bash

# QuantG VPS Quick Fix Script
# Fixes common VPS deployment issues
# Run this on your VPS: bash quick-fix.sh

echo "╔════════════════════════════════════════════════════════╗"
echo "║     QuantG VPS Quick Fix                               ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""

# Ensure we're in the right directory
if [ ! -f "docker-compose.yml" ]; then
    echo "ERROR: docker-compose.yml not found"
    echo "Run from: /app/quantg/QuantG"
    exit 1
fi

echo "[1/6] Stopping containers..."
docker-compose down
echo "✓ Done"

echo ""
echo "[2/6] Cleaning up Docker..."
docker system prune -a -f --volumes
echo "✓ Done"

echo ""
echo "[3/6] Configuring firewall..."
sudo ufw enable -y
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 3000/tcp
sudo ufw allow 8000/tcp
sudo ufw allow 27017/tcp
echo "✓ Firewall configured"

echo ""
echo "[4/6] Checking docker-compose.yml..."
# Check if ports are correct
if grep -q "127.0.0.1:3000" docker-compose.yml; then
    echo "WARNING: Found 127.0.0.1 binding in docker-compose.yml"
    echo "This will prevent external access"
    echo "Edit: nano docker-compose.yml"
    echo "Change: 127.0.0.1:3000 to 0.0.0.0:3000"
    echo "And: 127.0.0.1:8000 to 0.0.0.0:8000"
    exit 1
fi
echo "✓ docker-compose.yml looks OK"

echo ""
echo "[5/6] Starting containers..."
docker-compose up -d --build
echo "✓ Containers starting..."

echo ""
echo "[6/6] Waiting for containers to be ready..."
sleep 15

echo ""
echo "╔════════════════════════════════════════════════════════╗"
echo "║              Fix Complete!                             ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""

# Verify
echo "Verification:"
docker-compose ps
echo ""
echo "Testing localhost..."
curl -s http://localhost:3000 > /dev/null && echo "✓ Frontend OK" || echo "✗ Frontend failed"
curl -s http://localhost:8000/api/ > /dev/null && echo "✓ Backend OK" || echo "✗ Backend failed"
echo ""
echo "Your app should now be accessible at:"
echo "  http://YOUR_VPS_IP:3000"
echo ""
echo "Test from your laptop:"
echo "  curl http://82.180.145.183:3000"
