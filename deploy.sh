#!/bin/bash
# QuantG VPS Deployment Script — Contabo (82.180.145.183)
# Run this on your VPS to deploy everything

set -e

echo "================================"
echo "QuantG VPS Deployment Script"
echo "Target: Contabo 82.180.145.183"
echo "================================"
echo ""

# Check Docker is installed
if ! command -v docker &> /dev/null; then
    echo "❌ Docker not found. Install with:"
    echo "   curl -fsSL https://get.docker.com -o get-docker.sh && sh get-docker.sh"
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose not found. Install with:"
    echo "   sudo curl -L https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m) -o /usr/local/bin/docker-compose"
    echo "   sudo chmod +x /usr/local/bin/docker-compose"
    exit 1
fi

echo "✓ Docker is installed"
docker --version
docker-compose --version
echo ""

# Navigate to project
if [ ! -f "docker-compose.yml" ]; then
    echo "❌ docker-compose.yml not found. Are you in the QuantG directory?"
    exit 1
fi

echo "📦 Starting fresh deployment..."
echo ""

# Stop existing containers
echo "1️⃣  Stopping existing containers..."
docker-compose down --remove-orphans 2>/dev/null || true
echo "   ✓ Cleaned up"
echo ""

# Build images
echo "2️⃣  Building Docker images (this may take 5-10 minutes on first build)..."
docker-compose build --no-cache
echo "   ✓ Build complete"
echo ""

# Start services
echo "3️⃣  Starting services..."
docker-compose up -d
echo "   ✓ Services started"
echo ""

# Wait for services to be healthy
echo "4️⃣  Waiting for services to be ready..."
sleep 10
docker-compose logs --tail=20
echo ""

# Verify health
echo "5️⃣  Verifying deployment..."
echo ""
echo "Container status:"
docker ps --format "table {{.Names}}\t{{.Status}}"
echo ""

# Test endpoints
echo "6️⃣  Testing endpoints..."
echo ""

# Backend health
echo -n "   Backend health (http://localhost:8000/api/): "
if curl -s http://localhost:8000/api/ | grep -q "status"; then
    echo "✓ OK"
else
    echo "❌ FAILED"
fi

# Frontend
echo -n "   Frontend (http://localhost/): "
if curl -s http://localhost/ | grep -q "html\|React"; then
    echo "✓ OK"
else
    echo "❌ FAILED"
fi

echo ""
echo "================================"
echo "✅ DEPLOYMENT COMPLETE"
echo "================================"
echo ""
echo "🌐 Open your browser and go to:"
echo "   http://82.180.145.183:80"
echo ""
echo "📝 Next steps:"
echo "   1. Register / Login"
echo "   2. Go to Broker Keys"
echo "   3. Save Zerodha API credentials"
echo "   4. Click 'Connect to Zerodha'"
echo "   5. Complete Kite OAuth login"
echo "   6. Go to Strategies and click 'Seed Defaults'"
echo "   7. Select a strategy and click 'Test Run'"
echo "   8. Click 'Go Live' to start trading"
echo ""
echo "📊 Monitor logs:"
echo "   docker-compose logs -f backend"
echo "   docker-compose logs -f frontend"
echo ""
echo "🛑 To stop everything:"
echo "   docker-compose down"
echo ""
