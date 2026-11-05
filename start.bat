@echo off
setlocal EnableExtensions
rem Harborline 3PL Simulator - start everything and open the Control Center.
cd /d "%~dp0"
title Harborline 3PL Simulator

where docker >nul 2>&1
if errorlevel 1 (
  echo [FAIL] Docker is not installed or not on PATH. Install Docker Desktop, then run start.bat again.
  echo        See handbook\getting-started.html and run doctor.ps1 for help.
  pause
  exit /b 1
)

echo Checking that Docker Desktop is running...
docker info >nul 2>&1
if errorlevel 1 (
  echo [FAIL] Docker Desktop is not running.
  echo        Start Docker Desktop, wait until it shows "Engine running", then run start.bat again.
  pause
  exit /b 1
)

rem Makes the passwords and keys in .env the first time, and never changes them after that.
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup\fill-env.ps1"
if errorlevel 1 (
  pause
  exit /b 1
)
if not exist "landing" mkdir "landing"

set "HL_APP_PORT=8080"
for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
  if /i "%%A"=="HL_APP_PORT" set "HL_APP_PORT=%%B"
)
set "HL_URL=http://localhost:%HL_APP_PORT%"

echo Building and starting the containers (the first time takes a few minutes)...
docker compose up -d --build
if errorlevel 1 (
  echo.
  echo [FAIL] docker compose could not start the simulator.
  echo        Run:  powershell -ExecutionPolicy Bypass -File .\doctor.ps1
  echo        and:  docker compose logs --tail 100
  pause
  exit /b 1
)

echo Waiting for the simulator at %HL_URL% ...
set /a HL_TRIES=0
:wait
set /a HL_TRIES+=1
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 '%HL_URL%/api/health'; if ($r.StatusCode -eq 200) { exit 0 } } catch { }; exit 1" >nul 2>&1
if not errorlevel 1 goto ready
if %HL_TRIES% geq 150 (
  echo [WARN] The simulator did not answer within 5 minutes.
  echo        Check the logs with:  docker compose logs --tail 100 app
  pause
  exit /b 1
)
timeout /t 2 /nobreak >nul
goto wait

:ready
echo.
echo Harborline is running: %HL_URL%
echo   Control Center ... %HL_URL%/
echo   Handbook ......... %HL_URL%/START_HERE.html
echo   Stop it with stop.bat (your data is kept).
start "" "%HL_URL%/"
endlocal
exit /b 0
