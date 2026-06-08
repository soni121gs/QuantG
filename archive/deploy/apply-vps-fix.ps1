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

# The deployment file list (removed non-existent strategy_runner_v2.py)
$Files = @(
    "backend/server.py",
    "backend/position_reconciler.py",
    "backend/strategy_runner.py",
    "backend/core/market_domains.py",
    "backend/core/risk_manager.py",
    "docker-compose.yml",
    "frontend/Dockerfile.static",
    "frontend/src/contexts/AuthContext.jsx",
    "frontend/src/pages/Auth.jsx",
    "frontend/src/pages/AIBot.jsx",
    "frontend/src/pages/Dashboard.jsx",
    "frontend/src/lib/api.js",
    "frontend/.env.production",
    "frontend/nginx.conf",
    "frontend/build",
    "quantg_debug.sh"
)

# Robustness check: Ensure all files exist and are not empty paths
$ValidFiles = @()
foreach ($File in $Files) {
    if ([string]::IsNullOrWhiteSpace($File)) {
        continue
    }
    
    # Ignore temporary verifier files
    if ($File -like "*_integrity.json" -or $File -like "before_integrity.json" -or $File -like "during_integrity.json" -or $File -like "after_integrity.json") {
        Write-Host "Ignoring temporary verifier file: $File" -ForegroundColor Yellow
        continue
    }

    $FullPath = Join-Path $LocalRoot $File
    if (-not (Test-Path $FullPath)) {
        Write-Host "❌ CRITICAL ERROR: Required file/directory is missing: $File" -ForegroundColor Red
        throw "Missing required path for deployment: $File"
    }
    
    $ValidFiles += $File
}

if ($ValidFiles.Count -eq 0) {
    throw "No files to package."
}

Write-Host "Packaging $($ValidFiles.Count) files/directories..." -ForegroundColor Green
& tar -czf $Bundle -C $LocalRoot @ValidFiles
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create deploy bundle."
}

Write-Host "Uploading one deploy bundle to $Vps..."
scp "$Bundle" "${Vps}:/tmp/quantg-vps-fix.tgz"

Write-Host "Rebuilding and restarting QuantG on VPS..."
ssh $Vps "set -e; mkdir -p $RemoteRoot; tar -xzf /tmp/quantg-vps-fix.tgz -C $RemoteRoot; cp $RemoteRoot/quantg_debug.sh /tmp/quantg_debug.sh; chmod +x /tmp/quantg_debug.sh; cd $RemoteRoot; if docker compose version >/dev/null 2>&1; then COMPOSE='docker compose'; else COMPOSE='docker-compose'; fi; (ufw allow 80/tcp || true); `$COMPOSE down --remove-orphans || true; docker rm -f quantg-frontend quantg-backend quantg-mongo 2>/dev/null || true; `$COMPOSE up -d --build --force-recreate; `$COMPOSE ps; curl -I --max-time 10 http://localhost; docker logs quantg-backend --tail 40"

Write-Host "Checking public endpoints..."
$Checks = @(
    "https://www.quantgtrade.com",
    "https://www.quantgtrade.com/api/",
    "http://82.180.145.183",
    "http://82.180.145.183/api/"
)

foreach ($Url in $Checks) {
    try {
        $Response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 20
        $StatusCode = [int]$Response.StatusCode
        if ($StatusCode -in 200..399) {
            Write-Host "$Url -> HTTP $StatusCode (OK)" -ForegroundColor Green
        } else {
            Write-Host "$Url -> HTTP $StatusCode (Failed)" -ForegroundColor Red
        }
    }
    catch {
        $StatusCode = $null
        $ExceptionResponse = $null
        
        # Robustly extract the HTTP response regardless of PowerShell wrapping
        if ($_.Exception) {
            if ($_.Exception.Response) {
                $ExceptionResponse = $_.Exception.Response
            } elseif ($_.Exception.InnerException -and $_.Exception.InnerException.Response) {
                $ExceptionResponse = $_.Exception.InnerException.Response
            }
        }
        
        if ($ExceptionResponse) {
            $StatusCode = [int]$ExceptionResponse.StatusCode
            if ($StatusCode -in 200..399) {
                Write-Host "$Url -> HTTP $StatusCode (OK Redirect)" -ForegroundColor Green
            } else {
                Write-Host "$Url -> HTTP $StatusCode (Failed)" -ForegroundColor Red
                Write-Warning "Endpoint returned error code: $StatusCode"
            }
        } else {
            # Text-based fallback to catch 308/redirects inside the exception string
            $ErrorMessage = $_.ToString()
            if ($ErrorMessage -like "*(308)*" -or $ErrorMessage -like "*Permanent Redirect*") {
                Write-Host "$Url -> HTTP 308 (OK Redirect)" -ForegroundColor Green
            } else {
                Write-Host "$Url -> Connection Failed: $ErrorMessage" -ForegroundColor Yellow
            }
        }
    }
}

Write-Host "Done. Test in browser: https://www.quantgtrade.com"
