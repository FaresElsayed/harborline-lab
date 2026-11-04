@echo off
setlocal EnableExtensions
rem Delete ALL simulator data and start from scratch next time.
cd /d "%~dp0"
echo.
echo  This deletes ALL Harborline data:
echo    - the source databases (WMS, AX 2012, IoT, Salesforce and carrier stores)
echo    - the tracker (ticket statuses, comments, work logs, sprints)
echo    - files in the landing folder and answer_key\anomalies.csv
echo  Tip: export a tracker backup first (Tracker, then Backup).
echo.
set "HL_CONFIRM="
set /p "HL_CONFIRM=Type YES to delete everything: "
if /i not "%HL_CONFIRM%"=="YES" (
  echo Cancelled. Nothing was deleted.
  exit /b 0
)
docker info >nul 2>&1
if errorlevel 1 (
  echo [FAIL] Docker Desktop is not running. Start it, then run reset.bat again.
  pause
  exit /b 1
)
docker compose down -v
if exist "landing" (
  rmdir /s /q "landing"
  mkdir "landing"
)
if exist "answer_key\anomalies.csv" del /q "answer_key\anomalies.csv"
echo Done. Run start.bat to start fresh, then click Build in the Control Center.
endlocal
