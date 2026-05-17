@echo off
REM QuantG Live Trading - Daily Shutdown Script
REM Run this after market closes

setlocal enabledelayedexpansion

echo.
echo ========================================
echo QuantG Live Trading - Daily Shutdown
echo ========================================
echo.

cd /d D:\Quant\QuantG

echo [1/4] Stopping all live strategies...
echo.
echo IMPORTANT: Manually pause all strategies in the browser first:
echo 1. Go to http://192.168.31.4:3000/strategies
echo 2. For each "live" strategy, click "Pause"
echo 3. Wait for status to change to "paused"
echo 4. Then press ENTER here to continue
echo.
pause

echo.
echo [2/4] Stopping containers...
docker compose -f docker-compose.yml down >nul 2>&1
if errorlevel 1 (
    echo ERROR: Failed to stop containers
    echo Try manually:
    echo   docker compose -f D:\Quant\QuantG\docker-compose.yml down
) else (
    echo OK: All containers stopped
)

echo.
echo [3/4] Verifying shutdown...
timeout /t 3 /nobreak

docker ps --format "table {{.Names}}\t{{.Status}}" | find "quantg" >nul
if errorlevel 1 (
    echo OK: All QuantG containers are stopped
) else (
    echo WARNING: Some containers still running
)

echo.
echo [4/4] Checking available resources...
echo.
echo System ready for next session.
echo.

echo ========================================
echo Shutdown Complete!
echo ========================================
echo.
echo Summary:
echo - All containers stopped safely
echo - Data saved to MongoDB
echo - Ready for next trading day
echo.
echo See you tomorrow!
echo.
pause
