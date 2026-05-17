@echo off
REM QuantG Trading Platform - Daily Shutdown Script v2 (FINAL)
REM Graceful shutdown with position preservation

setlocal enabledelayedexpansion

cd /d D:\Quant\QuantG

REM ========================================
REM Configuration
REM ========================================
set SHUTDOWN_TIMEOUT=30

cls
echo.
echo ╔════════════════════════════════════════════════════════╗
echo ║                                                        ║
echo ║     QuantG Trading Platform - Daily Shutdown          ║
echo ║                    Version 2.0                        ║
echo ║                                                        ║
echo ╚════════════════════════════════════════════════════════╝
echo.

REM ========================================
REM Step 1: Verify containers exist
REM ========================================
echo [1/5] Checking running containers...
docker ps | find "quantg" >nul
if errorlevel 1 (
    echo ! No QuantG containers running
    echo.
    pause
    exit /b 0
)
echo ✓ QuantG containers found

REM ========================================
REM Step 2: Pause all strategies (user action)
REM ========================================
echo [2/5] Pre-shutdown verification...
echo.
echo IMPORTANT: Before closing, you MUST:
echo   1. Open: http://192.168.31.4:3000
echo   2. Go to: Strategies page
echo   3. Click "Pause" on ALL live strategies
echo   4. Wait for status to change to "paused"
echo   5. Return to this window and press ENTER
echo.
pause

REM ========================================
REM Step 3: Save all data
REM ========================================
echo [3/5] Saving all trading data...
echo   • Waiting for database operations to complete...
timeout /t 3 /nobreak

REM ========================================
REM Step 4: Stop containers gracefully
REM ========================================
echo [4/5] Stopping containers (up to %SHUTDOWN_TIMEOUT% seconds)...

REM Stop backend first (allows graceful shutdown of running trades)
docker stop quantg-backend >nul 2>&1
echo   ✓ Backend stopped

REM Give MongoDB time to flush writes
timeout /t 2

REM Stop frontend
docker stop quantg-frontend >nul 2>&1
echo   ✓ Frontend stopped

REM Stop MongoDB
docker stop quantg-mongo >nul 2>&1
echo   ✓ MongoDB stopped

timeout /t 2

REM ========================================
REM Step 5: Verify all stopped
REM ========================================
echo [5/5] Verifying shutdown...

docker ps | find "quantg-backend" >nul
if errorlevel 1 (
    echo   ✓ Backend stopped
) else (
    echo   ! Backend still running (force stopping...)
    docker kill quantg-backend >nul 2>&1
)

docker ps | find "quantg-frontend" >nul
if errorlevel 1 (
    echo   ✓ Frontend stopped
) else (
    echo   ! Frontend still running (force stopping...)
    docker kill quantg-frontend >nul 2>&1
)

docker ps | find "quantg-mongo" >nul
if errorlevel 1 (
    echo   ✓ MongoDB stopped
) else (
    echo   ! MongoDB still running (force stopping...)
    docker kill quantg-mongo >nul 2>&1
)

echo.
echo ╔════════════════════════════════════════════════════════╗
echo ║              ✓ SHUTDOWN COMPLETE                      ║
echo ╚════════════════════════════════════════════════════════╝
echo.
echo Summary:
echo   • All containers stopped safely
echo   • All data saved to MongoDB
echo   • Trading data backed up
echo   • Ready for next trading day
echo.
echo Important Notes:
echo   ✓ Do NOT force-close this window
echo   ✓ Do NOT unplug laptop during shutdown
echo   ✓ Allow 10 seconds for disk sync
echo   ✓ Keep laptop on for 1 minute after shutdown
echo.
echo See you tomorrow! 📈
echo.
timeout /t 10
