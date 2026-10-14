@echo off
REM Hope solver restart switch: kills the old solver (port 8077) and starts fresh.
echo [*] Killing old Hope solver on port 8077...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8077" ^| findstr "LISTENING"') do (
    echo [*] Killing PID %%a
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul
echo [*] Starting fresh Hope solver...
call "%~dp0run.bat"
