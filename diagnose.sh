#!/bin/bash

# QuantG VPS Quick Diagnosis Script
# Run this on your VPS to identify the issue quickly
# Usage: bash diagnose.sh

echo "╔════════════════════════════════════════════════════════╗"
echo "║     QuantG VPS Diagnostic Tool                         ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Check 1: Docker
echo "[1/10] Checking Docker..."
if command -v docker &> /dev/null; then
    echo -e "${GREEN}✓ Docker installed${NC}"
else
    echo -e "${RED}✗ Docker NOT installed${NC}"
    exit 1
fi

# Check 2: Docker Compose
echo "[2/10] Checking Docker Compose..."
if command -v docker-compose &> /dev/null; then
    echo -e "${GREEN}✓ Docker Compose installed${NC}"
else
    echo -e "${RED}✗ Docker Compose NOT installed${NC}"
    exit 1
fi

# Check 3: Project directory
echo "[3/10] Checking project directory..."
if [ -f "docker-compose.yml" ]; then
    echo -e "${GREEN}✓ docker-compose.yml found${NC}"
else
    echo -e "${RED}✗ docker-compose.yml NOT found${NC}"
    echo "Run: cd /app/quantg/QuantG"
    exit 1
fi

# Check 4: Container status
echo "[4/10] Checking container status..."
CONTAINERS=$(docker-compose ps --format "table {{.Names}}\t{{.Status}}" 2>/dev/null)
if echo "$CONTAINERS" | grep -q "Up"; then
    echo -e "${GREEN}✓ Containers running${NC}"
    echo "$CONTAINERS"
else
    echo -e "${RED}✗ Containers NOT running${NC}"
    echo "Run: docker-compose up -d"
    exit 1
fi

# Check 5: Frontend accessibility
echo ""
echo "[5/10] Checking frontend on localhost..."
if curl -s http://localhost:3000 > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Frontend accessible on localhost:3000${NC}"
else
    echo -e "${RED}✗ Frontend NOT accessible on localhost:3000${NC}"
    echo "Issue: Container not responding"
    echo "Run: docker-compose logs quantg-frontend"
fi

# Check 6: Backend accessibility
echo "[6/10] Checking backend on localhost..."
if curl -s http://localhost:8000/api/ > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Backend accessible on localhost:8000${NC}"
else
    echo -e "${RED}✗ Backend NOT accessible on localhost:8000${NC}"
    echo "Issue: Container not responding"
    echo "Run: docker-compose logs quantg-backend"
fi

# Check 7: Port bindings
echo "[7/10] Checking port bindings..."
if netstat -tlnp 2>/dev/null | grep -q ":3000 "; then
    echo -e "${GREEN}✓ Port 3000 is listening${NC}"
else
    echo -e "${RED}✗ Port 3000 NOT listening${NC}"
    echo "Run: docker-compose ps"
fi

if netstat -tlnp 2>/dev/null | grep -q ":8000 "; then
    echo -e "${GREEN}✓ Port 8000 is listening${NC}"
else
    echo -e "${RED}✗ Port 8000 NOT listening${NC}"
fi

# Check 8: Firewall
echo "[8/10] Checking firewall..."
if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    echo -e "${GREEN}✓ Firewall is active${NC}"
    
    if sudo ufw status | grep -q "3000/tcp"; then
        echo -e "${GREEN}✓ Port 3000 allowed${NC}"
    else
        echo -e "${RED}✗ Port 3000 NOT allowed${NC}"
        echo "Run: sudo ufw allow 3000/tcp"
    fi
    
    if sudo ufw status | grep -q "8000/tcp"; then
        echo -e "${GREEN}✓ Port 8000 allowed${NC}"
    else
        echo -e "${RED}✗ Port 8000 NOT allowed${NC}"
        echo "Run: sudo ufw allow 8000/tcp"
    fi
else
    echo -e "${YELLOW}⚠ Firewall not active (might be OK if cloud has it)${NC}"
fi

# Check 9: Environment file
echo "[9/10] Checking environment file..."
if [ -f ".env" ]; then
    echo -e "${GREEN}✓ .env file exists${NC}"
    
    if grep -q "REACT_APP_BACKEND_URL" .env; then
        BACKEND_URL=$(grep "REACT_APP_BACKEND_URL" .env | cut -d '=' -f 2)
        echo "  Backend URL: $BACKEND_URL"
    else
        echo -e "${YELLOW}⚠ REACT_APP_BACKEND_URL not in .env${NC}"
    fi
else
    echo -e "${RED}✗ .env file NOT found${NC}"
fi

# Check 10: Docker network
echo "[10/10] Checking Docker network..."
if docker network inspect quantg_quantg-network > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Docker network exists${NC}"
else
    echo -e "${YELLOW}⚠ Docker network might not be named correctly${NC}"
fi

echo ""
echo "╔════════════════════════════════════════════════════════╗"
echo "║                 Diagnosis Complete                     ║"
echo "╚════════════════════════════════════════════════════════╝"
echo ""

# Summary
echo "SUMMARY:"
if curl -s http://localhost:3000 > /dev/null 2>&1 && \
   curl -s http://localhost:8000/api/ > /dev/null 2>&1; then
    echo -e "${GREEN}✓ All systems operational on localhost${NC}"
    echo ""
    echo "If http://YOUR_VPS_IP:3000 doesn't work:"
    echo "1. Check firewall: sudo ufw status"
    echo "2. Check port binding: netstat -tlnp | grep docker"
    echo "3. Test from your laptop: curl http://YOUR_VPS_IP:3000"
else
    echo -e "${RED}✗ Issues found, check above for fixes${NC}"
fi
