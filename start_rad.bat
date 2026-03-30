@echo off
cd /d "%~dp0"
echo Starting Radiology Node (Port 8002)...
cd /d rad
start "RAD Node (Doctor)" cmd /k "..\clinical_venv\Scripts\python.exe node.py"
