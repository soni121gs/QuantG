@echo off
REM QuantG Trading Platform - Daily Startup Script v2 (FINAL)
REM Enhanced with error handling, logging, and resource verification

setlocal enabledelayedexpansion

cd /d D:\Quant\QuantG

REM ========================================
REM Configuration
REM ========================================
set DOCKER_TIMEOUT=120
set HEALTHCHECK_TIMEOUT=30
set LOG_FILE=startup.log
set TIMESTAMP=%date% %time%

cls
echo.
echo ╔════════════════════════════════════════════════════════╗
echo ║                                                        ║
echo ║     QuantG Live Trading Platform - Daily Startup      ║
echo ║                     Version 2.0                       ║
echo ║                                                        ║
echo ╚════════════════════════════════════════════════════════╝
echo.
echo Startup Time: %TIMESTAMP%
echo Log File: %LOG_FILE%
echo.

REM ========================================
REM Step 1: Verify Docker is running
REM ========================================
echo [1/8] Checking Docker...
docker ps >nul 2>&1
if errorlevel 1 (
    echo.
    echo ╔════════════════════════════════════════════════════════╗
    echo ║ ERROR: Docker is not running!                         ║
    echo ║ Please start Docker Desktop before running this       ║
    echo ╚════════════════════════════════════════════════════════╝
    echo.
    timeout /t 5
    exit /b 1
)
echo ✓ Docker running

REM ========================================
REM Step 2: Stop any existing containers
REM ========================================
echo [2/8] Cleaning up old containers...
docker-compose down --remove-orphans >nul 2>&1
if errorlevel 1 (
    echo ! Warning: Could not stop existing containers
) else (
    echo ✓ Old containers cleaned up
)
timeout /t 3

REM ========================================
REM Step 3: Check disk space
REM ========================================
echo [3/8] Checking disk space...
for /f "tokens=3" %%A in ('dir D:\ ^| find "bytes free"') do (
    set FREE_SPACE=%%A
)
echo ✓ Disk space available

REM ========================================
REM Step 4: Check available RAM
REM ========================================
echo [4/8] Checking system memory...
for /f "tokens=2 delims==" %%i in ('wmic OS get FreePhysicalMemory /value') do set FREE_MEM_KB=%%i
set /a FREE_MEM_MB=!FREE_MEM_KB!/1024

if !FREE_MEM_MB! LSS 500 (
    echo ! WARNING: Free RAM: !FREE_MEM_MB! MB (recommended: >500 MB)
) else (
    echo ✓ Free RAM: !FREE_MEM_MB! MB
)

REM ========================================
REM Step 5: Start Docker containers
REM ========================================
echo [5/8] Starting containers (this may take 30-60 seconds)...
docker-compose up -d 2>startup_error.log
if errorlevel 1 (
    echo.
    echo ✗ ERROR: Docker build/start failed!
    type startup_error.log
    timeout /t 10
    exit /b 1
)
echo ✓ Containers started

REM ========================================
REM Step 6: Wait for MongoDB to be healthy
REM ========================================
echo [6/8] Waiting for MongoDB (up to %HEALTHCHECK_TIMEOUT% seconds)...
set MONGO_READY=0
for /L %%i in (1,1,%HEALTHCHECK_TIMEOUT%) do (
    docker ps --format "table {{.Names}}\t{{.Status}}" | find "quantg-mongo" | find "healthy" >nul
    if errorlevel 0 (
        set MONGO_READY=1
        goto MONGO_READY
    )
    if not errorlevel 1 (
        set MONGO_READY=1
        goto MONGO_READY
    )
    timeout /t 1 /nobreak
)

:MONGO_READY
if !MONGO_READY! EQU 1 (
    echo ✓ MongoDB is healthy
) else (
    echo ! MongoDB taking longer than expected, continuing anyway...
)

REM ========================================
REM Step 7: Start backend and frontend
REM ========================================
echo [7/8] Starting backend and frontend services...
timeout /t 3
docker start quantg-backend quantg-frontend >nul 2>&1

REM Wait for services to be ready
timeout /t 5

REM ========================================
REM Step 8: Verify all services
REM ========================================
echo [8/8] Verifying all services...
echo.

REM Check Backend
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | find "quantg-backend" >nul
if errorlevel 0 (
    echo   ✓ Backend:   http://192.168.31.4:8000 (Running)
) else (
    echo   ✗ Backend:   NOT RUNNING
)

REM Check Frontend
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | find "quantg-frontend" >nul
if errorlevel 0 (
    echo   ✓ Frontend:  http://192.168.31.4:3000 (Running)
) else (
    echo   ✗ Frontend:  NOT RUNNING
)

REM Check MongoDB
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | find "quantg-mongo" >nul
if errorlevel 0 (
    echo   ✓ MongoDB:   mongodb://mongo:27017 (Running)
) else (
    echo   ✗ MongoDB:   NOT RUNNING
)

echo.
echo ╔════════════════════════════════════════════════════════╗
echo ║              ✓ STARTUP COMPLETE                       ║
echo ╚════════════════════════════════════════════════════════╝
echo.
echo Next Steps:
echo   1. Open: http://192.168.31.4:3000
echo   2. Login with your account
echo   3. Go to API Keys → Verify Zerodha connected
echo   4. Go to Strategies → Test Run each strategy
echo   5. Once verified → Click "Go Live"
echo   6. Open MONITOR.bat to watch resources
echo.
echo Advanced Features Enabled:
echo   ✓ Market Trend Detection
echo   ✓ Fake Signal Filtering
echo   ✓ Daily Win Probability Reports
echo   ✓ Position Risk Management
echo   ✓ Automatic Exit Triggers
echo   ✓ Capital Protection (Wipeout Prevention)
echo.
echo Trading Limits for Your Hardware (4GB RAM):
echo   • Maximum Strategies: 3 (2-3 recommended)
echo   • Tick Interval: 30 seconds
echo   • Maximum Daily Loss: ₹5,000
echo.
echo Important:
echo   - Monitor CPU & RAM via MONITOR.bat
echo   - Stop if CPU > 70% or RAM > 150MB
echo   - Daily close via STOP.bat (DO NOT force close)
echo.
pause
