@echo off
REM QuantG Live Trading - Real-time Monitoring
REM Run this during market hours to watch system health

setlocal enabledelayedexpansion

:loop
cls
echo.
echo ================================================================================
echo QuantG Live Trading - Real-time Monitoring
echo ================================================================================
echo.
echo Timestamp: %date% %time%
echo.

echo [CONTAINERS STATUS]
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Size}}" | findstr /c:"quantg"
if errorlevel 1 (
    echo ERROR: Some containers are not running!
)

echo.
echo [RESOURCE USAGE]
docker stats --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"

echo.
echo [BACKEND HEALTH]
curl -s http://localhost:8000/api/ | find "QuantG API" >nul
if errorlevel 1 (
    echo ALERT: Backend is not responding!
) else (
    echo OK: Backend is responding
)

echo.
echo [ALERTS]
REM Check memory usage
for /f "tokens=*" %%i in ('docker stats --no-stream --format "{{.MemPerc}}" quantg-backend') do (
    set mem=%%i
    set mem=!mem:%%=!
    if !mem! GTR 8 (
        echo WARNING: Backend memory usage ^> 8%% 
    )
)

for /f "tokens=*" %%i in ('docker stats --no-stream --format "{{.MemPerc}}" quantg-mongo') do (
    set mem=%%i
    set mem=!mem:%%=!
    if !mem! GTR 8 (
        echo WARNING: MongoDB memory usage ^> 8%% 
    )
)

echo.
echo [LIVE STRATEGIES]
echo Open browser to check:
echo - http://192.168.31.4:3000/strategies
echo - Look for green "SCANNING" indicators
echo - "Last scan" should update every 30 seconds
echo.

echo [OPTIONS]
echo Press ENTER to refresh (auto-refreshes every 15 seconds)
echo Type "exit" and press ENTER to stop monitoring
echo.
set /p choice=

if /i "%choice%"=="exit" goto end
if /i "%choice%"=="stop" goto end

timeout /t 15 /nobreak
goto loop

:end
echo.
echo Monitoring stopped.
echo.
pause
