@echo off
setlocal enabledelayedexpansion
title Ember Forge Setup
color 0E

echo.
echo   ============================================
echo     EMBER FORGE - One-Click Setup
echo     every voice carries weight
echo   ============================================
echo.

set "INSTALL_DIR=%USERPROFILE%\EmberForge"
set "PYTHON_DIR=%INSTALL_DIR%\python"
set "GAME_DIR=%INSTALL_DIR%\game"
set "PYTHON_URL=https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip"
set "PIP_URL=https://bootstrap.pypa.io/get-pip.py"
set "REPO_ZIP=https://github.com/kuhlmanne/WIngman/archive/refs/heads/main.zip"

:: Create install directory
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

:: Check if already installed
if exist "%GAME_DIR%\studio_server.py" (
    echo   Ember Forge is already installed.
    echo   Launching...
    goto :launch
)

:: Step 1: Download Python embeddable
echo   [1/4] Downloading Python...
if not exist "%PYTHON_DIR%" mkdir "%PYTHON_DIR%"
powershell -Command "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%INSTALL_DIR%\python.zip'" 2>nul
if errorlevel 1 (
    echo   Failed to download Python. Check your internet connection.
    pause
    exit /b 1
)
echo   Extracting Python...
powershell -Command "Expand-Archive -Path '%INSTALL_DIR%\python.zip' -DestinationPath '%PYTHON_DIR%' -Force" 2>nul
del "%INSTALL_DIR%\python.zip" 2>nul

:: Enable pip in embeddable Python (uncomment import site)
for %%f in ("%PYTHON_DIR%\python*._pth") do (
    powershell -Command "(Get-Content '%%f') -replace '#import site','import site' | Set-Content '%%f'"
)
echo   Python ready.

:: Step 2: Install pip
echo   [2/4] Installing pip...
powershell -Command "Invoke-WebRequest -Uri '%PIP_URL%' -OutFile '%INSTALL_DIR%\get-pip.py'" 2>nul
"%PYTHON_DIR%\python.exe" "%INSTALL_DIR%\get-pip.py" --quiet 2>nul
del "%INSTALL_DIR%\get-pip.py" 2>nul

:: Install anthropic SDK
echo   Installing game dependencies...
"%PYTHON_DIR%\python.exe" -m pip install anthropic --quiet 2>nul
echo   Dependencies ready.

:: Step 3: Download game
echo   [3/4] Downloading Ember Forge...
powershell -Command "Invoke-WebRequest -Uri '%REPO_ZIP%' -OutFile '%INSTALL_DIR%\game.zip'" 2>nul
if errorlevel 1 (
    echo   Failed to download game. Check your internet connection.
    pause
    exit /b 1
)
echo   Extracting game...
powershell -Command "Expand-Archive -Path '%INSTALL_DIR%\game.zip' -DestinationPath '%INSTALL_DIR%\temp' -Force" 2>nul

:: Move from nested folder to game dir
for /d %%d in ("%INSTALL_DIR%\temp\*") do (
    if exist "%GAME_DIR%" rmdir /s /q "%GAME_DIR%"
    move "%%d" "%GAME_DIR%" >nul
)
rmdir /s /q "%INSTALL_DIR%\temp" 2>nul
del "%INSTALL_DIR%\game.zip" 2>nul
echo   Game ready.

:: Step 4: Create desktop shortcut
echo   [4/4] Creating shortcut...
set "SHORTCUT=%USERPROFILE%\Desktop\Ember Forge.lnk"
set "LAUNCHER=%INSTALL_DIR%\launch.bat"

:: Create launcher bat
(
echo @echo off
echo title Ember Forge
echo cd /d "%GAME_DIR%"
echo "%PYTHON_DIR%\python.exe" studio_server.py
) > "%LAUNCHER%"

:: Create shortcut via PowerShell
powershell -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut('%SHORTCUT%'); $s.TargetPath='%LAUNCHER%'; $s.WorkingDirectory='%GAME_DIR%'; $s.Description='Ember Forge'; $s.Save()" 2>nul
echo   Shortcut created on Desktop.

echo.
echo   ============================================
echo     Setup complete!
echo   ============================================
echo.

:launch
echo   Starting Ember Forge...
cd /d "%GAME_DIR%"
start "" "%PYTHON_DIR%\python.exe" -c "import webbrowser,time; time.sleep(3); webbrowser.open('http://localhost:7070/studio')"
"%PYTHON_DIR%\python.exe" studio_server.py
pause
