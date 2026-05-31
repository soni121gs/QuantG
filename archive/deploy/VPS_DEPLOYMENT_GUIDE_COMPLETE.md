# QuantG VPS Deployment Guide - Complete Setup

**What You Need to Know:**
- This guide covers moving QuantG from your laptop to a VPS (Virtual Private Server)
- VPS = Server running in cloud 24/7, always accessible
- Split: What I pre-did vs What you need to do

---

## 📋 Part 1: What's Already Done (Pre-Configured)

✅ **Docker containerization** - All code is already in Docker images
✅ **Environment variables** - All configs are externalized (.env files)
✅ **Database setup** - MongoDB configured for cloud databases
✅ **API structure** - RESTful APIs ready for remote access
✅ **Frontend optimization** - Responsive design for remote access

---

## 🖥️ Part 2: VPS Selection & Setup (What You Do)

### Step 1: Choose a VPS Provider

**Recommended Providers:**
```
1. AWS EC2 (Recommended)
   - Free tier: 750 hours/month (covers 1 server 24/7)
   - Pay as you go after free tier
   - $5-20/month for small instance
   - Link: https://aws.amazon.com/ec2/

2. DigitalOcean (Easiest)
   - Simple, beginner-friendly
   - $6/month minimum
   - Link: https://www.digitalocean.com/

3. Linode
   - $6/month minimum
   - Simple interface
   - Link: https://www.linode.com/

4. Vultr
   - $2.50-$5/month
   - Pay hourly
   - Link: https://www.vultr.com/
```

**For QuantG: I recommend DigitalOcean or AWS (free tier)**

### Step 2: Create VPS Instance

**DigitalOcean Steps:**
```
1. Sign up: https://www.digitalocean.com/
2. Click "Create Droplet"
3. Choose:
   - Region: Singapore or Mumbai (closest to India)
   - Image: Ubuntu 22.04 LTS
   - Size: $6/month (Basic 1 GB RAM)
   - Click "Create Droplet"
4. Get IP address (example: 123.45.67.89)
5. Check email for root password
```

**AWS EC2 Steps:**
```
1. Sign up: https://aws.amazon.com/
2. Go to EC2 Dashboard
3. Click "Launch Instance"
4. Choose:
   - AMI: Ubuntu Server 22.04 LTS
   - Instance type: t2.micro (free tier)
   - Create new key pair (save .pem file)
   - Security group: Allow SSH (22), HTTP (80), HTTPS (443)
5. Launch and get IP address
6. Download key pair file
```

### Step 3: Connect to VPS via SSH

**On Windows (using PuTTY or PowerShell):**

```bash
# Using PowerShell (Windows 10+)
ssh -i "path/to/key.pem" root@YOUR_VPS_IP

# Example:
ssh -i "C:\Users\You\aws-key.pem" root@123.45.67.89
```

**On Mac/Linux:**
```bash
ssh -i /path/to/key.pem root@YOUR_VPS_IP
```

**DigitalOcean (password-based):**
```bash
ssh root@YOUR_VPS_IP
# Enter password from email
```

### Step 4: Install Prerequisites on VPS

Once connected via SSH, run these commands:

```bash
# Update system
apt update && apt upgrade -y

# Install Docker
apt install -y docker.io docker-compose

# Install Git
apt install -y git

# Give docker permission (so no sudo needed)
usermod -aG docker root

# Install curl & wget
apt install -y curl wget

# Reboot to apply changes
reboot
```

**After reboot, reconnect and verify:**
```bash
docker --version
docker-compose --version
git --version
```

---

## 🚀 Part 3: Deploy QuantG on VPS (What You Do)

### Step 1: Clone QuantG from GitHub

```bash
# Create app directory
mkdir -p /app/quantg
cd /app/quantg

# Clone your repo
# (You'll need to create GitHub repo first - see below)
git clone https://github.com/YOUR_USERNAME/quantg.git .

# Or if using private repo:
git clone https://your-private-url.git .
```

**If you don't have GitHub repo yet:**
```bash
# 1. Create repo on GitHub.com
# 2. On VPS, initialize git:
cd /app/quantg
git init

# 3. Add remote:
git remote add origin https://github.com/YOUR_USERNAME/quantg.git

# 4. Pull code:
git pull origin main

# Or push from laptop:
# On your laptop in QuantG folder:
git remote add origin https://github.com/YOUR_USERNAME/quantg.git
git push -u origin main
```

### Step 2: Create/Update Environment Files on VPS

```bash
# Create .env file
cd /app/quantg
nano .env

# Add these (modify as needed):
```

**Content for .env:**
```
# Backend
JWT_SECRET=your-secret-key-here-change-this
EMERGENT_LLM_KEY=sk-xxx-your-key-here
DB_NAME=quantg
MONGO_URL=mongodb://mongo:27017

# Frontend
REACT_APP_BACKEND_URL=http://YOUR_VPS_IP:8000
CORS_ORIGINS=http://YOUR_VPS_IP:3000,http://localhost:3000,http://YOUR_VPS_IP:8000
```

**Save (Ctrl+O, Enter, Ctrl+X)**

### Step 3: Update docker-compose.yml for VPS

```bash
# Edit docker-compose.yml on VPS
nano docker-compose.yml

# Make these changes:
```

**Key changes:**
```yaml
# Change ports to expose on VPS
services:
  backend:
    ports:
      - "8000:8000"    # Change 0.0.0.0 to just port
    environment:
      - MONGO_URL=mongodb://mongo:27017
  
  frontend:
    ports:
      - "3000:8080"
    environment:
      - REACT_APP_BACKEND_URL=http://YOUR_VPS_IP:8000
  
  mongo:
    ports:
      - "27017:27017"
    volumes:
      - mongo_data:/data/db
    environment:
      - MONGO_INITDB_ROOT_USERNAME=admin
      - MONGO_INITDB_ROOT_PASSWORD=change-this-password

volumes:
  mongo_data:
```

### Step 4: Start Services on VPS

```bash
# Go to project directory
cd /app/quantg

# Start all containers
docker-compose up -d

# Wait 30 seconds for services to start

# Check status
docker-compose ps

# View logs
docker-compose logs -f

# Stop with Ctrl+C
```

### Step 5: Verify VPS Access

```bash
# From your laptop, test:
curl http://YOUR_VPS_IP:8000/api/

# Should return:
# {"status":"ok","service":"QuantG API"}

# Open in browser:
http://YOUR_VPS_IP:3000
```

---

## 🔧 Part 4: Domain Setup (Optional but Recommended)

### Get a Domain
```
1. Buy domain from:
   - Namecheap.com (~$1/year)
   - GoDaddy.com
   - Domain.com

2. Point domain to VPS IP:
   - Go to DNS settings
   - Add A record: your-domain.com → YOUR_VPS_IP
   - Wait 10-15 minutes for DNS to propagate

3. Access: http://your-domain.com:3000
```

### Enable HTTPS (Free with Let's Encrypt)

```bash
# Install Certbot
apt install -y certbot python3-certbot-nginx

# Get certificate
certbot certonly --standalone -d your-domain.com

# Update docker-compose to use certificate
# (Advanced setup - ask if needed)
```

---

## 📊 Part 5: Ongoing VPS Management

### Monitor VPS Resources

```bash
# Check disk space
df -h

# Check memory
free -m

# Check CPU
top
# Press q to exit

# Check Docker container sizes
docker system df

# Clean up unused images
docker system prune -a
```

### Backup MongoDB Data

```bash
# Create backup directory
mkdir -p /backups

# Backup MongoDB
docker exec quantg-mongo mongodump --out /dump
docker cp quantg-mongo:/dump /backups/mongo-backup-$(date +%Y%m%d).tar.gz

# List backups
ls -lh /backups/

# Download to laptop (from laptop):
scp -r root@YOUR_VPS_IP:/backups/ ./local-backups/
```

### Update Code on VPS

```bash
cd /app/quantg

# Pull latest from GitHub
git pull origin main

# Rebuild and restart
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

### Check Logs

```bash
# Backend logs
docker-compose logs quantg-backend -f

# Frontend logs
docker-compose logs quantg-frontend -f

# MongoDB logs
docker-compose logs quantg-mongo -f
```

---

## 🛡️ Part 6: Security Setup (Important)

### Configure Firewall

```bash
# Enable firewall
ufw enable

# Allow SSH (don't lock yourself out!)
ufw allow 22/tcp

# Allow HTTP
ufw allow 80/tcp

# Allow HTTPS
ufw allow 443/tcp

# Allow custom ports
ufw allow 3000/tcp
ufw allow 8000/tcp
ufw allow 27017/tcp

# Check status
ufw status
```

### Secure MongoDB

```bash
# Edit docker-compose.yml
nano docker-compose.yml

# Add password and auth:
environment:
  - MONGO_INITDB_ROOT_USERNAME=admin
  - MONGO_INITDB_ROOT_PASSWORD=strong-password-here

# Restart
docker-compose restart quantg-mongo
```

### Change SSH Password

```bash
# Change root password
passwd

# Create non-root user (recommended)
adduser tradingbot
usermod -aG docker tradingbot
usermod -aG sudo tradingbot
```

---

## 📈 Part 7: Scaling on VPS

### If You Hit Resource Limits

**Monitor:**
```bash
# Watch real-time stats
docker stats --no-stream
```

**Upgrade VPS:**
```
If hitting limits:
1. Upgrade to 2GB RAM ($12-15/month)
2. Upgrade to 4GB RAM ($20-30/month)
3. Use managed MongoDB (don't run in Docker)
   - AWS RDS MongoDB
   - MongoDB Atlas (free 512MB)
```

### Enable Auto-Restart on VPS Reboot

```bash
# Edit docker-compose.yml
# Add restart policy:
services:
  backend:
    restart: always
  frontend:
    restart: always
  mongo:
    restart: always

# Restart services
docker-compose up -d
```

---

## 🔄 Part 8: Comparison - Laptop vs VPS

| Feature | Laptop | VPS |
|---------|--------|-----|
| **Always On** | ❌ Need laptop on | ✅ 24/7 running |
| **Accessible** | 📱 WiFi only | 🌍 From anywhere |
| **Max Strategies** | 2-3 | 5-10+ |
| **Uptime** | 95% | 99.9% |
| **Cost** | ₹0 | ₹300-600/month |
| **Setup** | Easy | Medium |
| **Maintenance** | Manual | Automated |

---

## 💾 Part 9: Final Checklist

### Before Going Live on VPS

- [ ] VPS instance running
- [ ] Docker & docker-compose installed
- [ ] QuantG code cloned/pushed to VPS
- [ ] .env file configured with VPS IP
- [ ] docker-compose.yml updated
- [ ] All 3 containers running (`docker-compose ps`)
- [ ] Frontend accessible at http://YOUR_VPS_IP:3000
- [ ] Backend responding at http://YOUR_VPS_IP:8000/api/
- [ ] Can login to frontend
- [ ] Zerodha API keys configured
- [ ] MongoDB backup created
- [ ] Firewall configured
- [ ] SSH secured (strong password / key-based)
- [ ] Paper trade 5 tests on VPS
- [ ] Ready for LIVE trading

---

## 🚨 Troubleshooting on VPS

### "Can't connect to VPS"
```bash
# Check if instance is running
# Ping VPS: ping YOUR_VPS_IP
# SSH might need key file: ssh -i key.pem root@IP
```

### "Services not starting"
```bash
# Check logs
docker-compose logs

# Restart
docker-compose restart

# Full rebuild
docker-compose down
docker-compose up -d --build
```

### "Out of memory"
```bash
# Check usage
free -m
docker system df

# Stop some containers
docker-compose stop quantg-frontend

# Clean up
docker system prune -a
```

### "Cannot access from laptop"
```bash
# Check firewall
ufw status

# Allow ports
ufw allow 3000/tcp
ufw allow 8000/tcp

# Check if services running
docker-compose ps
```

---

## 📞 VPS Hosting Resources

**Quick Setup Guides:**
- DigitalOcean Docker: https://docs.digitalocean.com/products/app-platform/
- AWS EC2: https://docs.aws.amazon.com/ec2/
- Linode Docker: https://www.linode.com/docs/guides/docker/

**Learning:**
- SSH Tutorial: https://www.digitalocean.com/community/tutorials/how-to-use-ssh
- Docker on VPS: https://docs.docker.com/engine/install/ubuntu/
- MongoDB: https://docs.mongodb.com/manual/

---

## 📋 Step-by-Step Summary (Quick Reference)

```
1. CHOOSE VPS → Sign up (DigitalOcean $6/month)
2. CREATE INSTANCE → Ubuntu 22.04 LTS
3. INSTALL DOCKER → apt install docker.io docker-compose
4. CLONE CODE → git clone quantg repo
5. UPDATE .env → Set VPS_IP and MONGO password
6. START SERVICES → docker-compose up -d
7. TEST → Open http://VPS_IP:3000
8. CONFIGURE → Firewall, SSH, backups
9. LIVE → Connect Zerodha and trade 24/7
```

---

## 🎊 You're Ready!

Once VPS is set up:
- QuantG runs 24/7 without your laptop
- Strategies execute automatically
- Accessible from phone/browser anywhere
- Professional setup like Tradetron

**Cost: ₹300-600/month for peace of mind**

---

**Questions? Check Docker docs or ask your VPS provider's support.**

