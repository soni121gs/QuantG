@echo off
REM QuantG Live Trading - Daily Startup Script
REM Run this every morning before market opens

setlocal enabledelayedexpansion

echo.
echo ========================================
echo QuantG Live Trading - Daily Startup
echo ========================================
echo.

REM Check if Docker is running
echo [1/7] Checking Docker...
docker ps >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker is not running!
    echo Please start Docker Desktop first.
    pause
    exit /b 1
)
echo OK: Docker is running

REM Navigate to project directory
cd /d D:\Quant\QuantG
if errorlevel 1 (
    echo ERROR: Cannot navigate to D:\Quant\QuantG
    pause
    exit /b 1
)
echo OK: Project directory found

REM Stop old containers
echo.
echo [2/7] Stopping old containers...
docker-compose down --remove-orphans >nul 2>&1
echo OK: Old containers stopped

REM Start fresh containers
echo.
echo [3/7] Starting containers...
docker-compose up -d >nul 2>&1
if errorlevel 1 (
    echo ERROR: Failed to start containers
    pause
    exit /b 1
)
echo OK: Containers started

REM Wait for MongoDB to be healthy
echo.
echo [4/7] Waiting for MongoDB to be healthy (15 seconds)...
timeout /t 15 /nobreak

REM Manually start backend and frontend if they didn't start automatically
echo.
echo [5/7] Checking backend and frontend...
docker ps | find "quantg-backend" >nul
if errorlevel 1 (
    echo WARNING: Backend not running, starting it now...
    docker start quantg-backend
)
docker ps | find "quantg-frontend" >nul
if errorlevel 1 (
    echo WARNING: Frontend not running, starting it now...
    docker start quantg-frontend
)

REM Wait a bit more
echo.
echo [6/7] Waiting for services to be ready (5 seconds)...
timeout /t 5 /nobreak

REM Show container status
echo.
echo [7/7] Container Status:
echo.
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo.
echo ========================================
echo Startup Complete!
echo ========================================
echo.
echo Your app is ready:
echo - Frontend: http://192.168.31.4:3000
echo - Backend:  http://192.168.31.4:8000/api/
echo - Laptop:   http://localhost:3000
echo.
echo Next steps:
echo 1. Open http://192.168.31.4:3000 in your browser
echo 2. Login to your account
echo 3. Check API Keys section - verify Zerodha is connected
echo 4. Go to Strategies and click "Test Run" for each strategy
echo 5. Once tests pass, click "Go Live"
echo 6. Monitor the "Last scan" column - should update every 30s
echo.
echo IMPORTANT: 
echo - Run maximum 2-3 strategies
echo - If CPU/RAM alerts appear, pause strategies immediately
echo - Never close this window until market closes
echo.
pause
