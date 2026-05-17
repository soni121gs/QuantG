$ErrorActionPreference = "Stop"

$Vps = "root@82.180.145.183"
$RemoteRoot = "/app/quantg/QuantG"
$LocalRoot = "D:\Quant\QuantG"
$Bundle = Join-Path $env:TEMP "quantg-vps-fix.tgz"

Write-Host "Building frontend locally for fast, reliable VPS deploy..."
Push-Location "$LocalRoot\frontend"
try {
    npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend build failed."
    }
}
finally {
    Pop-Location
}

Write-Host "Packing fixes..."
if (Test-Path -LiteralPath $Bundle) {
    Remove-Item -LiteralPath $Bundle -Force
}

$Files = @(
    "backend/server.py",
    "backend/strategy_runner.py",
    "backend/strategy_runner_v2.py",
    "docker-compose.yml",
    "frontend/Dockerfile.static",
    "frontend/src/contexts/AuthContext.jsx",
    "frontend/src/pages/Auth.jsx",
    "frontend/src/pages/AIBot.jsx",
    "frontend/src/lib/api.js",
    "frontend/.env.production",
    "frontend/nginx.conf",
    "frontend/build",
    "quantg_debug.sh"
)

& tar -czf $Bundle -C $LocalRoot @Files
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create deploy bundle."
}

Write-Host "Uploading one deploy bundle to $Vps..."
scp "$Bundle" "${Vps}:/tmp/quantg-vps-fix.tgz"

Write-Host "Rebuilding and restarting QuantG on VPS..."
ssh $Vps "set -e; mkdir -p $RemoteRoot; tar -xzf /tmp/quantg-vps-fix.tgz -C $RemoteRoot; cp $RemoteRoot/quantg_debug.sh /tmp/quantg_debug.sh; chmod +x /tmp/quantg_debug.sh; cd $RemoteRoot; if docker compose version >/dev/null 2>&1; then COMPOSE='docker compose'; else COMPOSE='docker-compose'; fi; (ufw allow 80/tcp || true); `$COMPOSE down --remove-orphans || true; docker rm -f quantg-frontend quantg-backend quantg-mongo 2>/dev/null || true; `$COMPOSE up -d --build --force-recreate; `$COMPOSE ps; curl -I --max-time 10 http://localhost; docker logs quantg-backend --tail 40"

Write-Host "Checking public endpoints..."
$Checks = @(
    "http://82.180.145.183",
    "http://82.180.145.183/api/",
    "http://82.180.145.183:8000/docs"
)

foreach ($Url in $Checks) {
    $Response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 20
    Write-Host "$Url -> HTTP $($Response.StatusCode)"
}

Write-Host "Done. Test in browser: http://82.180.145.183"
