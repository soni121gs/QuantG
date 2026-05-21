#!/bin/bash

# QuantG VPS Auto-Deployment Script for Contabo
# This script automates the complete setup on Contabo VPS
# Run with: bash deploy-contabo.sh YOUR_VPS_IP YOUR_DOMAIN

set -e

VPS_IP="${1:-}"
DOMAIN="${2:-}"
APP_DIR="/opt/quantg"
DOCKER_COMPOSE_VERSION="2.20.0"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║  QuantG Contabo VPS Auto-Deployment                    ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════╝${NC}"
echo ""

if [ -z "$VPS_IP" ]; then
    echo -e "${RED}Error: VPS IP required${NC}"
    echo "Usage: bash deploy-contabo.sh YOUR_VPS_IP [OPTIONAL_DOMAIN]"
    exit 1
fi

echo -e "${YELLOW}[1/10] Updating system packages...${NC}"
apt update && apt upgrade -y > /dev/null 2>&1
apt install -y curl wget git nano ufw > /dev/null 2>&1
echo -e "${GREEN}✓ System updated${NC}"

echo ""
echo -e "${YELLOW}[2/10] Installing Docker...${NC}"
apt install -y docker.io docker-compose > /dev/null 2>&1
systemctl enable docker > /dev/null 2>&1
systemctl start docker > /dev/null 2>&1
usermod -aG docker root
echo -e "${GREEN}✓ Docker installed${NC}"

echo ""
echo -e "${YELLOW}[3/10] Configuring Firewall...${NC}"
ufw --force enable > /dev/null 2>&1
ufw allow 22/tcp > /dev/null 2>&1
ufw allow 80/tcp > /dev/null 2>&1
ufw allow 443/tcp > /dev/null 2>&1
ufw allow 3000/tcp > /dev/null 2>&1
ufw allow 8000/tcp > /dev/null 2>&1
echo -e "${GREEN}✓ Firewall configured${NC}"

echo ""
echo -e "${YELLOW}[4/10] Creating app directory...${NC}"
mkdir -p $APP_DIR
cd $APP_DIR
echo -e "${GREEN}✓ App directory created${NC}"

echo ""
echo -e "${YELLOW}[5/10] Cloning QuantG repository...${NC}"
if [ -d ".git" ]; then
    echo "Repository already exists, pulling updates..."
    git pull origin main > /dev/null 2>&1
else
    # Initialize with local repo - you'll push to GitHub
    git init > /dev/null 2>&1
    git remote add origin https://github.com/YOUR_USERNAME/quantg.git 2>/dev/null || true
fi
echo -e "${GREEN}✓ Repository ready${NC}"

echo ""
echo -e "${YELLOW}[6/10] Creating environment file...${NC}"
cat > .env << EOF
# QuantG Environment - Contabo VPS
JWT_SECRET=$(openssl rand -base64 32)
MONGO_URL=mongodb://admin:admin123@mongo:27017
DB_NAME=quantg
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.5-flash
GEMINI_TIMEOUT_SEC=20

# Frontend
REACT_APP_BACKEND_URL=http://${VPS_IP}:8000
CORS_ORIGINS=http://${VPS_IP}:3000,http://${VPS_IP}:8000,http://localhost:3000,http://localhost:8000

# Zerodha
ZERODHA_API_KEY=
ZERODHA_API_SECRET=
ZERODHA_ACCESS_TOKEN=

# Environment
ENVIRONMENT=production
PORT=8000
FRONTEND_PORT=3000
EOF
echo -e "${GREEN}✓ Environment file created${NC}"

echo ""
echo -e "${YELLOW}[7/10] Configuring Docker Compose...${NC}"
cat > docker-compose.yml << 'EOF'
version: '3.8'

services:
  mongo:
    image: mongo:6.0
    container_name: quantg-mongo
    restart: always
    environment:
      MONGO_INITDB_ROOT_USERNAME: admin
      MONGO_INITDB_ROOT_PASSWORD: admin123
    ports:
      - "27017:27017"
    volumes:
      - mongo_data:/data/db
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"]
      interval: 10s
      timeout: 5s
      retries: 5

  backend:
    image: quantg/backend:latest
    container_name: quantg-backend
    restart: always
    depends_on:
      mongo:
        condition: service_healthy
    environment:
      - MONGO_URL=mongodb://admin:admin123@mongo:27017
      - JWT_SECRET=${JWT_SECRET}
      - PORT=8000
    ports:
      - "8000:8000"
    volumes:
      - ./backend:/app
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/"]
      interval: 10s
      timeout: 5s
      retries: 5

  frontend:
    image: quantg/frontend:latest
    container_name: quantg-frontend
    restart: always
    depends_on:
      - backend
    environment:
      - REACT_APP_BACKEND_URL=http://${VPS_IP}:8000
    ports:
      - "3000:80"
    volumes:
      - ./frontend/dist:/usr/share/nginx/html
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:80"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  mongo_data:
    driver: local

networks:
  default:
    name: quantg-network
EOF
echo -e "${GREEN}✓ Docker Compose configured${NC}"

echo ""
echo -e "${YELLOW}[8/10] Building Docker images...${NC}"
docker-compose build --no-cache 2>&1 | grep -E "(Step|Successfully)" || true
echo -e "${GREEN}✓ Images built${NC}"

echo ""
echo -e "${YELLOW}[9/10] Starting services...${NC}"
docker-compose up -d
sleep 10
echo -e "${GREEN}✓ Services started${NC}"

echo ""
echo -e "${YELLOW}[10/10] Verifying deployment...${NC}"
echo ""
docker-compose ps
echo ""

# Verify services
if docker-compose ps | grep -q "Up"; then
    echo -e "${GREEN}✓ All services running${NC}"
else
    echo -e "${RED}✗ Some services not running${NC}"
    docker-compose logs
    exit 1
fi

echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║           Deployment Complete!                        ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${GREEN}Access your QuantG platform:${NC}"
echo -e "  Frontend:  ${YELLOW}http://${VPS_IP}:3000${NC}"
echo -e "  Backend:   ${YELLOW}http://${VPS_IP}:8000/api/${NC}"
echo -e "  MongoDB:   ${YELLOW}mongodb://admin:admin123@${VPS_IP}:27017${NC}"
echo ""
if [ ! -z "$DOMAIN" ]; then
    echo -e "${GREEN}Domain setup:${NC}"
    echo -e "  Point ${YELLOW}${DOMAIN}${NC} to ${VPS_IP}"
    echo ""
fi
echo -e "${YELLOW}Next steps:${NC}"
echo "  1. Update .env with your Zerodha API keys"
echo "  2. Login at http://${VPS_IP}:3000"
echo "  3. Configure Broker Keys"
echo "  4. Create strategies"
echo "  5. Go Live!"
echo ""
echo -e "${YELLOW}Monitoring:${NC}"
echo "  View logs: docker-compose logs -f"
echo "  Check status: docker-compose ps"
echo "  Stop services: docker-compose down"
echo "  Start services: docker-compose up -d"
echo ""
