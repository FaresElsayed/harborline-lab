@echo off
setlocal EnableExtensions
rem Stop the simulator. All data (databases, tracker progress, landing files) is kept.
cd /d "%~dp0"
docker info >nul 2>&1
if errorlevel 1 (
  echo Docker Desktop is not running, so the simulator is already stopped.
  exit /b 0
)
echo Stopping Harborline (data is kept)...
docker compose down
if errorlevel 1 (
  echo [FAIL] docker compose down failed. See the message above.
  pause
  exit /b 1
)
echo Stopped. Run start.bat to start it again.
endlocal
