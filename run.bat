@echo off
REM Hope solver - Windows starter. Double-click this file.
REM Needs Python 3.13+ installed (tick "Add python to PATH" during setup).
cd /d "%~dp0our_solver"
where python >nul 2>nul
if errorlevel 1 (
    echo [Hope] Python not found. Install it from https://www.python.org/downloads/
    echo [Hope] Tick "Add python.exe to PATH" during setup, then run this again.
    pause
    exit /b 1
)
echo [Hope] Installing dependencies (first run takes a few minutes)...
python -m pip install --quiet -r ..\requirements.txt
python -m playwright install chromium
echo [Hope] Starting solver on http://127.0.0.1:8077 ...
set SOLVER_WORKERS=2
python -m sites.manus.solver_api --host 127.0.0.1 --port 8077
pause
