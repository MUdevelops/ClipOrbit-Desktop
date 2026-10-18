@echo off
setlocal
cd /d "%~dp0"
set "PY="
py -3.11 -c "import sys;sys.exit(0 if sys.version_info[:3]==(3,11,9) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3.11"
if not defined PY (
    python -c "import sys;sys.exit(0 if sys.version_info[:3]==(3,11,9) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [ClipOrbit] Python 3.11.9 was not found.
    echo Install it from https://www.python.org/downloads/release/python-3119/
    echo and tick "Add python.exe to PATH" during setup, then run start.bat again.
    pause
    exit /b 1
)
%PY% -c "import PySide6, yt_dlp" >nul 2>&1
if errorlevel 1 (
    echo [ClipOrbit] Installing required packages from requirements.txt ...
    %PY% -m pip install --upgrade pip
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ClipOrbit] Package installation failed. Check your internet connection and try again.
        pause
        exit /b 1
    )
)
%PY% main.py
if errorlevel 1 (
    echo [ClipOrbit] The application exited with an error.
    pause
)
endlocal
