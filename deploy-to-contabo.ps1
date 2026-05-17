# QuantG Contabo VPS One-Click Deployment
# PowerShell script to auto-deploy to your Contabo VPS
# Usage: .\deploy-to-contabo.ps1 -VpsIP "123.45.67.89" -Domain "yourdomain.com" -SSHKey "C:\path\to\key.pem"

param(
    [Parameter(Mandatory=$true)]
    [string]$VpsIP,
    
    [Parameter(Mandatory=$false)]
    [string]$Domain = "",
    
    [Parameter(Mandatory=$false)]
    [string]$SSHKey = "",
    
    [Parameter(Mandatory=$false)]
    [string]$SSHUser = "root"
)

Write-Host "╔════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║     QuantG Contabo VPS One-Click Deployment           ║" -ForegroundColor Cyan
Write-Host "╚════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# Validate VPS IP
if (-not ($VpsIP -match '^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')) {
    Write-Host "ERROR: Invalid VPS IP address" -ForegroundColor Red
    exit 1
}

# Check if SSH key exists
if ($SSHKey -and -not (Test-Path $SSHKey)) {
    Write-Host "ERROR: SSH key not found at $SSHKey" -ForegroundColor Red
    exit 1
}

Write-Host "[1/5] Testing VPS connection..." -ForegroundColor Yellow
try {
    if ($SSHKey) {
        # Using SSH key
        ssh -i $SSHKey "${SSHUser}@${VpsIP}" "echo 'Connection successful'" | Out-Null
    } else {
        # Using password (will prompt)
        ssh "${SSHUser}@${VpsIP}" "echo 'Connection successful'" 2>$null | Out-Null
    }
    Write-Host "✓ VPS connection successful" -ForegroundColor Green
} catch {
    Write-Host "✗ Failed to connect to VPS" -ForegroundColor Red
    Write-Host "Make sure you can SSH to ${SSHUser}@${VpsIP}" -ForegroundColor Yellow
    exit 1
}

Write-Host ""
Write-Host "[2/5] Uploading deployment script..." -ForegroundColor Yellow
if ($SSHKey) {
    scp -i $SSHKey ".\deploy-contabo.sh" "${SSHUser}@${VpsIP}:/tmp/deploy.sh" 2>$null
} else {
    scp ".\deploy-contabo.sh" "${SSHUser}@${VpsIP}:/tmp/deploy.sh" 2>$null
}
Write-Host "✓ Script uploaded" -ForegroundColor Green

Write-Host ""
Write-Host "[3/5] Running deployment on VPS..." -ForegroundColor Yellow
Write-Host "(This may take 5-10 minutes, please wait...)" -ForegroundColor Yellow
Write-Host ""

$deployCmd = "bash /tmp/deploy.sh $VpsIP $Domain"
if ($SSHKey) {
    ssh -i $SSHKey "${SSHUser}@${VpsIP}" $deployCmd
} else {
    ssh "${SSHUser}@${VpsIP}" $deployCmd
}

Write-Host ""
Write-Host "[4/5] Verifying deployment..." -ForegroundColor Yellow
$statusCmd = "docker-compose -f /opt/quantg/docker-compose.yml ps"
if ($SSHKey) {
    ssh -i $SSHKey "${SSHUser}@${VpsIP}" $statusCmd
} else {
    ssh "${SSHUser}@${VpsIP}" $statusCmd
}
Write-Host "✓ Deployment verified" -ForegroundColor Green

Write-Host ""
Write-Host "[5/5] Getting access details..." -ForegroundColor Yellow
Write-Host ""
Write-Host "╔════════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║         Deployment Complete!                          ║" -ForegroundColor Green
Write-Host "╚════════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""
Write-Host "Access your QuantG platform:" -ForegroundColor Green
Write-Host "  Frontend:  http://${VpsIP}:3000" -ForegroundColor Cyan
Write-Host "  Backend:   http://${VpsIP}:8000/api/" -ForegroundColor Cyan
Write-Host ""
Write-Host "First steps:" -ForegroundColor Yellow
Write-Host "  1. Open http://${VpsIP}:3000 in browser"
Write-Host "  2. Login with your account"
Write-Host "  3. Go to Broker Keys → Add Zerodha API credentials"
Write-Host "  4. Go to Strategies → Create or enable strategies"
Write-Host "  5. Click 'Go Live' to start trading!"
Write-Host ""
Write-Host "SSH access to VPS:" -ForegroundColor Yellow
Write-Host "  ssh ${SSHUser}@${VpsIP}" -ForegroundColor Cyan
Write-Host ""
Write-Host "View logs:" -ForegroundColor Yellow
Write-Host "  docker-compose -f /opt/quantg/docker-compose.yml logs -f" -ForegroundColor Cyan
Write-Host ""
Write-Host "Stop services:" -ForegroundColor Yellow
Write-Host "  docker-compose -f /opt/quantg/docker-compose.yml down" -ForegroundColor Cyan
Write-Host ""
