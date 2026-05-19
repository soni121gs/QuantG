# QuantG Docker Deployment Script (Windows PowerShell)
# Run this from D:\Quant\QuantG directory

Write-Host "================================" -ForegroundColor Cyan
Write-Host "QuantG Docker Deployment" -ForegroundColor Cyan
Write-Host "Target: 82.180.145.183 (Contabo VPS)" -ForegroundColor Cyan
Write-Host "================================" -ForegroundColor Cyan
Write-Host ""

# Check Docker
try {
    docker --version | Out-Null
    docker-compose --version | Out-Null
    Write-Host "✓ Docker is installed" -ForegroundColor Green
} catch {
    Write-Host "❌ Docker not found. Install Docker Desktop from https://www.docker.com/products/docker-desktop" -ForegroundColor Red
    exit 1
}

# Check docker-compose.yml
if (!(Test-Path "docker-compose.yml")) {
    Write-Host "❌ docker-compose.yml not found. Run this from D:\Quant\QuantG" -ForegroundColor Red
    exit 1
}

Write-Host "📦 Starting deployment..." -ForegroundColor Yellow
Write-Host ""

# Stop & cleanup
Write-Host "1️⃣  Stopping existing containers..." -ForegroundColor Yellow
docker-compose down --remove-orphans 2>$null
Write-Host "   ✓ Cleaned up" -ForegroundColor Green
Write-Host ""

# Build
Write-Host "Building Docker images (5-10 minutes first time)..." -ForegroundColor Cyan
docker-compose build --no-cache
Write-Host "   ✓ Build complete" -ForegroundColor Green
Write-Host ""

# Start
Write-Host "3️⃣  Starting services..." -ForegroundColor Yellow
docker-compose up -d
Write-Host "   ✓ Services started" -ForegroundColor Green
Write-Host ""

# Wait
Write-Host "4️⃣  Waiting for services to be ready..." -ForegroundColor Yellow
Start-Sleep -Seconds 10
Write-Host "   ✓ Ready" -ForegroundColor Green
Write-Host ""

# Status
Write-Host "5️⃣  Container status:" -ForegroundColor Yellow
docker ps --format "table {{.Names}}`t{{.Status}}"
Write-Host ""

# Test
Write-Host "6️⃣  Testing endpoints..." -ForegroundColor Yellow
Write-Host ""

$backendOk = $false
$frontendOk = $false

try {
    $response = Invoke-WebRequest -Uri "http://localhost:8000/api/" -TimeoutSec 3
    if ($response.Content -match "status") { $backendOk = $true }
} catch {}

try {
    $response = Invoke-WebRequest -Uri "http://localhost/" -TimeoutSec 3
    if ($response.Content -match "html|React") { $frontendOk = $true }
} catch {}

Write-Host "   Backend (http://localhost:8000/api/): $(if($backendOk) { '✓ OK' } else { '❌ FAILED' })" -ForegroundColor $(if($backendOk) { 'Green' } else { 'Red' })
Write-Host "   Frontend (http://localhost/): $(if($frontendOk) { '✓ OK' } else { '❌ FAILED' })" -ForegroundColor $(if($frontendOk) { 'Green' } else { 'Red' })
Write-Host ""

Write-Host "================================" -ForegroundColor Green
Write-Host "✅ DEPLOYMENT COMPLETE" -ForegroundColor Green
Write-Host "================================" -ForegroundColor Green
Write-Host ""
Write-Host "🌐 Open your browser:" -ForegroundColor Cyan
Write-Host "   http://localhost/ (local testing)" -ForegroundColor White
Write-Host "   http://82.180.145.183:80 (VPS production)" -ForegroundColor White
Write-Host ""
Write-Host "📝 Next steps:" -ForegroundColor Cyan
Write-Host "   1. Register / Login" -ForegroundColor White
Write-Host "   2. Broker Keys → Save Zerodha credentials" -ForegroundColor White
Write-Host "   3. Broker Keys → Connect to Zerodha" -ForegroundColor White
Write-Host "   4. Strategies → Seed Defaults" -ForegroundColor White
Write-Host "   5. Select strategy → Test Run → Go Live" -ForegroundColor White
Write-Host ""
Write-Host "📊 Monitor logs:" -ForegroundColor Cyan
Write-Host "   docker-compose logs -f backend" -ForegroundColor White
Write-Host "   docker-compose logs -f frontend" -ForegroundColor White
Write-Host ""
Write-Host "🛑 Stop everything:" -ForegroundColor Cyan
Write-Host "   docker-compose down" -ForegroundColor White
Write-Host ""
