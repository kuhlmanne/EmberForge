@echo off
title Ember Forge
echo.
echo   ==============================
echo     AMPLIFY - Ember Forge
echo     every voice carries weight
echo   ==============================
echo.

cd /d "%~dp0"

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo   Python not found. Install Python 3.10+ from python.org
    pause
    exit /b 1
)

:: Install anthropic SDK if missing
python -c "import anthropic" >nul 2>&1
if errorlevel 1 (
    echo   Installing required packages...
    pip install anthropic -q
)

:: Check if studio is already running
python -c "import socket; s=socket.socket(); r=s.connect_ex(('127.0.0.1',7070)); s.close(); exit(0 if r==0 else 1)" >nul 2>&1
if errorlevel 1 (
    echo   Starting Ember Forge server...
    start /b pythonw studio_server.py
    timeout /t 3 /nobreak >nul
) else (
    echo   Server already running.
)

:: Open browser
echo   Opening browser...
start http://localhost:7070

echo.
echo   Ember Forge is running at http://localhost:7070
echo   Other devices on your network: http://%COMPUTERNAME%:7070/studio
echo.
echo   Press any key to stop the server.
pause >nul

:: Kill the server
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":7070.*LISTENING"') do taskkill /PID %%a /F >nul 2>&1
echo   Server stopped.
