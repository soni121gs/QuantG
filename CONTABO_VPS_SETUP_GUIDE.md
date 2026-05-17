# QuantG on Contabo VPS - Complete Setup Guide

**Status:** ✅ One-Click Deployment Ready
**VPS Provider:** Contabo
**Cost:** ₹300-500/month ($4-6 USD)
**Uptime:** 99.9%

---

## 🚀 QUICK START (30 MINUTES)

### Step 1: Get Your Contabo VPS Details

1. **Contabo Account:**
   - Go to https://contabo.com
   - Buy VPS: Ubuntu 22.04 LTS (4GB RAM, €4.99/month)
   - Get: IP Address, Root Password, SSH Port

2. **You'll receive:**
   ```
   IP Address: 123.45.67.89
   Root Password: abc123xyz (save this!)
   SSH Port: 22
   ```

### Step 2: Prepare Deployment

On your **laptop**, save your Contabo details:

```
VPS_IP=123.45.67.89
SSH_USER=root
SSH_PASSWORD=abc123xyz
DOMAIN=yourdomain.com (optional)
```

### Step 3: One-Click Deploy

**Option A: Using PowerShell (Windows)**

```powershell
# Open PowerShell in QuantG directory
cd D:\Quant\QuantG

# Run deployment
.\deploy-to-contabo.ps1 -VpsIP "123.45.67.89" -Domain "optional-domain.com"

# It will prompt for password if no SSH key
# Just enter your Contabo root password
```

**Option B: Using SSH Key (More Secure)**

```powershell
# First, create SSH key pair (one-time)
ssh-keygen -t rsa -b 4096 -f "C:\Users\YOU\.ssh\contabo_key"

# Copy public key to Contabo (one-time)
scp -P 22 "C:\Users\YOU\.ssh\contabo_key.pub" root@123.45.67.89:/tmp/
# SSH to VPS and run:
ssh root@123.45.67.89
cat /tmp/contabo_key.pub >> ~/.ssh/authorized_keys

# Then use deployment script:
.\deploy-to-contabo.ps1 -VpsIP "123.45.67.89" -SSHKey "C:\Users\YOU\.ssh\contabo_key"
```

### Step 4: Access Your App

```
After deployment completes:

Frontend:  http://123.45.67.89:3000
Backend:   http://123.45.67.89:8000/api/
```

---

## 📋 DETAILED SETUP (STEP-BY-STEP)

### Step 1: Create Contabo Account

1. Go to https://contabo.com
2. Click "VPS"
3. Choose:
   - **OS:** Ubuntu 22.04 LTS
   - **Region:** Europe (for lower latency to India via EU)
   - **Plan:** VPS M (€4.99/month) - 4GB RAM
4. Complete payment
5. Check email for VPS details

### Step 2: Note Your Contabo Details

You'll receive email with:
```
VPS IP:              123.45.67.89
Root Username:       root
Root Password:       XyZ123aBc (save this!)
SSH Port:            22
Control Panel:       https://my.contabo.com
```

### Step 3: First SSH Connection

**From Windows PowerShell:**
```powershell
# First connection (will ask for password)
ssh root@123.45.67.89

# When prompted for password, enter from email
# Accept host key (type 'yes')
```

**You should see:**
```
Welcome to Ubuntu 22.04 LTS
root@server:~#
```

### Step 4: Clone QuantG Code to VPS

```bash
# On VPS, clone your repository
mkdir -p /opt/quantg
cd /opt/quantg
git clone https://github.com/YOUR_USERNAME/quantg.git .

# OR if you don't have GitHub repo yet:
cd /opt/quantg
git init
```

### Step 5: Run Automated Deployment

```bash
# Download and run deployment script
bash deploy-contabo.sh 123.45.67.89 yourdomain.com

# Sit back and wait 5-10 minutes for full setup
```

### Step 6: Verify Installation

```bash
# Check if all containers are running
docker-compose -f /opt/quantg/docker-compose.yml ps

# Should show 3 running:
# - quantg-mongo     Up (healthy)
# - quantg-backend   Up (healthy)
# - quantg-frontend  Up (healthy)
```

### Step 7: Update Environment File

```bash
# SSH into VPS
ssh root@123.45.67.89

# Edit .env file
nano /opt/quantg/.env

# Find these lines and update with YOUR values:
ZERODHA_API_KEY=your-api-key
ZERODHA_API_SECRET=your-api-secret
ZERODHA_ACCESS_TOKEN=your-access-token

# Save (Ctrl+O, Enter, Ctrl+X)

# Restart services
docker-compose -f /opt/quantg/docker-compose.yml restart backend
```

### Step 8: Access Application

Open in browser:
```
http://123.45.67.89:3000
```

Login with:
- Email: your-email@example.com
- Password: your-password

---

## 🔧 DAILY OPERATIONS

### Monitor Services

```bash
# SSH into VPS
ssh root@123.45.67.89

# Go to app directory
cd /opt/quantg

# View running services
docker-compose ps

# View logs
docker-compose logs -f

# Stop logs (Ctrl+C)
```

### Backup Database

```bash
# Backup MongoDB
docker exec quantg-mongo mongodump --out /backup
docker cp quantg-mongo:/backup ./backup-$(date +%Y%m%d)

# Download to laptop
scp -r root@123.45.67.89:/opt/quantg/backup-* ./backups/
```

### Update Code

```bash
cd /opt/quantg

# Pull latest from GitHub
git pull origin main

# Rebuild
docker-compose build --no-cache
docker-compose up -d

# Verify
docker-compose ps
```

### View Real-Time Stats

```bash
# CPU, Memory, Network usage
docker stats

# Disk space
df -h

# Memory
free -m
```

---

## 🛡️ SECURITY SETUP

### 1. Change Root Password

```bash
# SSH into VPS
ssh root@123.45.67.89

# Change password
passwd

# Enter new strong password twice
```

### 2. Create Non-Root User

```bash
# Create user
adduser tradingbot

# Make admin
usermod -aG sudo tradingbot
usermod -aG docker tradingbot

# Login as new user next time
ssh tradingbot@123.45.67.89
```

### 3. Setup SSH Key Authentication

```powershell
# On laptop, generate key (one-time)
ssh-keygen -t rsa -b 4096 -f ~/.ssh/contabo

# Copy to VPS
scp ~/.ssh/contabo.pub root@123.45.67.89:/tmp/

# On VPS, add key
ssh root@123.45.67.89
cat /tmp/contabo.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# Test login with key (from laptop)
ssh -i ~/.ssh/contabo root@123.45.67.89
# Should NOT ask for password now
```

### 4. Disable Root Login (After Setup)

```bash
# SSH as new user (tradingbot)
ssh tradingbot@123.45.67.89

# Edit SSH config
sudo nano /etc/ssh/sshd_config

# Find line: PermitRootLogin yes
# Change to: PermitRootLogin no

# Save and restart SSH
sudo systemctl restart ssh
```

### 5. Setup Firewall

```bash
# Enable firewall
sudo ufw enable

# Allow SSH
sudo ufw allow 22/tcp

# Allow HTTP
sudo ufw allow 80/tcp

# Allow HTTPS
sudo ufw allow 443/tcp

# Allow custom ports
sudo ufw allow 3000/tcp
sudo ufw allow 8000/tcp

# Check status
sudo ufw status
```

---

## 🔗 SETUP CUSTOM DOMAIN (OPTIONAL)

### 1. Buy Domain

- Buy from: namecheap.com, godaddy.com, or domains.google.com
- Cost: ~₹100-300/year

### 2. Point Domain to VPS IP

1. Login to domain registrar
2. Go to DNS settings
3. Add **A Record:**
   ```
   Name: @
   Type: A
   Value: 123.45.67.89
   TTL: 3600
   ```
4. Add **CNAME** for www (optional):
   ```
   Name: www
   Type: CNAME
   Value: yourdomain.com
   TTL: 3600
   ```
5. Save and wait 10-15 minutes for DNS propagation

### 3. Enable HTTPS

```bash
# SSH to VPS
ssh tradingbot@123.45.67.89

# Install Certbot
sudo apt install certbot python3-certbot-nginx

# Get certificate
sudo certbot certonly --standalone -d yourdomain.com

# Add to docker-compose for HTTPS
# (Advanced - ask if needed)
```

### 4. Access via Domain

```
http://yourdomain.com:3000
http://yourdomain.com:8000
```

---

## 📊 MONITORING & ALERTS

### Check Resources

```bash
# Real-time CPU and Memory
docker stats

# Disk usage
df -h

# Memory details
free -m

# Process monitoring
top
# Press q to exit
```

### Set Alerts

**Add to cron (auto-check every hour):**

```bash
# SSH to VPS
ssh tradingbot@123.45.67.89

# Edit crontab
crontab -e

# Add this line:
0 * * * * docker stats --no-stream | tail -1 >> /var/log/docker-stats.log

# Save (Ctrl+O, Enter, Ctrl+X)
```

---

## 🚨 TROUBLESHOOTING

### "Cannot connect to VPS"
```bash
# Check if you have IP and port correct
ssh -vvv root@YOUR_VPS_IP

# Check firewall on Contabo control panel
# Make sure ports 22, 80, 443, 3000, 8000 are allowed
```

### "Services not starting"
```bash
# SSH to VPS
ssh root@123.45.67.89

# View logs
docker-compose logs quantg-backend

# Restart all
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

# Clean up unused containers
docker system prune -a

# Upgrade VPS RAM on Contabo panel
```

### "Cannot access from browser"
```bash
# Check if services running
docker-compose ps

# Check firewall
sudo ufw status

# If port not allowed:
sudo ufw allow 3000/tcp
sudo ufw allow 8000/tcp

# Restart Docker
docker-compose restart
```

---

## 💾 BACKUP & RESTORE

### Full System Backup

```bash
# SSH to VPS
ssh root@123.45.67.89

# Create backup directory
mkdir -p /backups

# Backup MongoDB
docker exec quantg-mongo mongodump --out /tmp/mongo-backup
tar -czf /backups/mongo-$(date +%Y%m%d-%H%M%S).tar.gz /tmp/mongo-backup

# Backup code
tar -czf /backups/quantg-code-$(date +%Y%m%d).tar.gz /opt/quantg

# List backups
ls -lh /backups/
```

### Download Backups to Laptop

```powershell
# Download from VPS
scp -r root@123.45.67.89:/backups/ D:\Backups\Contabo-Quantg\
```

### Restore from Backup

```bash
# SSH to VPS
ssh root@123.45.67.89

# Restore MongoDB
tar -xzf /backups/mongo-BACKUP_DATE.tar.gz -C /
docker exec quantg-mongo mongorestore /tmp/mongo-backup

# Restore code
cd /opt
tar -xzf /backups/quantg-code-BACKUP_DATE.tar.gz

# Restart services
docker-compose -f /opt/quantg/docker-compose.yml restart
```

---

## 🎊 FINAL CHECKLIST

- [ ] Contabo VPS created
- [ ] VPS IP and password saved
- [ ] QuantG code cloned to laptop
- [ ] Deployment script ready
- [ ] One-click deployment completed
- [ ] All 3 services running (docker ps)
- [ ] Frontend accessible (http://VPS_IP:3000)
- [ ] Backend responding (http://VPS_IP:8000/api/)
- [ ] Zerodha API keys configured
- [ ] First strategy created and tested
- [ ] Paper traded 5+ times
- [ ] Ready for LIVE trading

---

## 📞 SUPPORT

**Contabo Support:**
- Website: https://contabo.com
- Email: support@contabo.com
- Phone: Available on website

**QuantG Support:**
- Check logs: `docker-compose logs`
- Rebuild: `docker-compose down && docker-compose up -d --build`
- Monitor: `docker stats`

---

## 🎯 NEXT STEPS

1. **Week 1:** Setup VPS, test paper trading
2. **Week 2:** Run live with 1 strategy
3. **Week 3:** Add 2nd strategy
4. **Week 4:** Scale to 3-4 strategies

**Your platform is now 24/7 live on Contabo! 🚀**

