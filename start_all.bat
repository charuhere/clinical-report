@echo off
echo ===================================================
echo   DISTRIBUTED CLINICAL REPORT SYSTEM - LAUNCHER
echo ===================================================
echo.

cd /d "%~dp0icu"
echo [1/3] Starting ICU Node (Port 8001)...
start "ICU Node (Doctor)" cmd /k "..\clinical_venv\Scripts\python.exe node.py"

cd /d "%~dp0rad"
echo [2/3] Starting Radiology Node (Port 8002)...
start "RAD Node (Doctor)" cmd /k "..\clinical_venv\Scripts\python.exe node.py"

cd /d "%~dp0dashboard"
echo [3/3] Starting Admin Dashboard (Port 4000)...
start "Admin Dashboard" cmd /k "node server.js"

cd /d "%~dp0"

echo.
echo Waiting a few seconds for servers to initialize...
timeout /t 4 /nobreak >nul

echo Opening browser tabs...
start http://localhost:8001
start http://localhost:8002

start http://localhost:4000

echo.
echo All services are running in separate background windows!
echo You can close this launcher window now.
pause
