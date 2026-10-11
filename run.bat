@echo off
title OWNASSET
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" (
    set "VENV=venv"
    goto install
)
if exist ".venv\Scripts\python.exe" (
    set "VENV=.venv"
    goto install
)

echo Creating virtual environment in .venv ...
python -m venv .venv
if errorlevel 1 (
    echo.
    echo [ERROR] Could not create the virtual environment.
    echo Install Python 3.10+ from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during installation.
    goto end
)
set "VENV=.venv"

:install
echo Installing requirements ...
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install requirements.
    goto end
)

echo.
echo Starting OWNASSET at http://127.0.0.1:5089  (close this window to stop)
rem delayed so the server is up before the browser loads the page
start "" /min cmd /c "timeout /t 4 /nobreak >nul & start "" http://127.0.0.1:5089"
"%VENV%\Scripts\python.exe" app.py

:end
echo.
pause
